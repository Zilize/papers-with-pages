"""Explore a bounded graph of repositories and project pages."""

import re
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Sequence
from urllib.parse import urldefrag

from pwp.errors import PwpError
from pwp.events import EventHandler, EventReporter
from pwp.extract import extract_metadata_links, merge_links, normalize_url
from pwp.github import inspect_repo, repository_name
from pwp.link_policy import admissible_child
from pwp.models import (
    DiscoveryLimits,
    DiscoveryResult,
    Paper,
    ProjectPage,
    ResourceLink,
    StepOutcome,
)
from pwp.network import RequestBudget, SafeFetcher, request_scope
from pwp.page import can_explore, inspect_page, seed_link

PageLoader = Callable[[str], Awaitable[ProjectPage]]


def resource_key(link: ResourceLink) -> str:
    if link.kind == "repository":
        try:
            return "https://github.com/" + repository_name(link.url).lower()
        except ValueError:
            pass
    return urldefrag(normalize_url(link.url))[0]


def _normalized(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.lower()))


def related_to_paper(paper: Paper, ids: Sequence[str], title: str = "") -> bool:
    """Associate a page that links only this paper, or links it and matches its title.

    Pages and READMEs often cite prior work too, so extra paper links do not break
    association when the title matches. Without that, a multi-paper page (such as a
    lab listing) stays a candidate.
    """
    expected = _normalized(paper.title)
    title_match = len(expected) >= 15 and expected in _normalized(title)
    if ids:
        return paper.arxiv_id in ids and (len(set(ids)) == 1 or title_match)
    return title_match


async def discover_resources(
    paper: Paper,
    *,
    links: Iterable[ResourceLink] | None = None,
    limits: DiscoveryLimits | None = None,
    fetcher: SafeFetcher | None = None,
    github_token: str | None = None,
    on_event: EventHandler | None = None,
    page_loader: PageLoader | None = None,
) -> DiscoveryResult:
    limits = limits or DiscoveryLimits()
    if fetcher is None:
        async with SafeFetcher(budget=RequestBudget(limits.max_requests)) as owned:
            return await discover_resources(
                paper,
                links=links,
                limits=limits,
                fetcher=owned,
                github_token=github_token,
                on_event=on_event,
                page_loader=page_loader,
            )
    budget = RequestBudget(limits.max_requests)
    with request_scope(budget):
        return await _discover(
            paper, links, limits, fetcher, github_token, on_event, page_loader, budget
        )


async def _discover(paper, links, limits, fetcher, github_token, on_event, page_loader, budget):
    reporter = EventReporter("discover_resources", on_event)
    reporter.emit("step_started", paper_id=paper.arxiv_id)
    resources = {}
    pending = deque()
    queued = set()
    visited = set()
    pages, repositories, steps = [], [], []
    limited = False
    inspected = 0

    def add(link, depth):
        nonlocal limited
        key = resource_key(link)
        if key in resources:
            previous = resources[key]
            combined = merge_links([previous, link.model_copy(update={"url": previous.url})])[0]
            updates = {}
            if previous.kind == "other" and link.kind != "other":
                updates["kind"] = link.kind
            if link.relation == "related":
                updates["relation"] = "related"
            resources[key] = combined.model_copy(update=updates)
        elif len(resources) >= limits.max_links:
            limited = True
            return
        else:
            resources[key] = link
            reporter.emit("resource_discovered", paper_id=paper.arxiv_id, resource_url=link.url)
        if can_explore(resources[key]) and key not in queued and key not in visited:
            if depth > limits.max_depth:
                # The depth bound is the normal exploration horizon, not an exhausted
                # budget, so it does not mark the result as limited or partial.
                return
            pending.append((key, depth))
            queued.add(key)

    for link in extract_metadata_links(paper) if links is None else links:
        add(seed_link(link), 0)
    while pending:
        if inspected >= limits.max_resources or budget.used >= limits.max_requests:
            limited = True
            break
        key, depth = pending.popleft()
        if key in visited:
            continue
        visited.add(key)
        inspected += 1
        link = resources[key]
        step = "inspect_repo" if link.kind == "repository" else "inspect_page"
        reporter.emit("step_progress", unit=step, resource_url=link.url)
        try:
            if link.kind == "repository":
                result = await inspect_repo(link.url, fetcher=fetcher, token=github_token)
                repositories.append(result)
                associated = related_to_paper(paper, result.related_paper_ids, result.description)
                children = result.links
                canonical = result.url
                if result.issues:
                    steps.append(
                        StepOutcome(
                            step=step,
                            status="partial",
                            resource_url=link.url,
                            issue=result.issues[0],
                        )
                    )
                    limited |= any(issue.code == "request_limit" for issue in result.issues)
                else:
                    steps.append(StepOutcome(step=step, status="completed", resource_url=link.url))
            else:
                result = (
                    await page_loader(link.url)
                    if page_loader
                    else await inspect_page(link.url, fetcher=fetcher)
                )
                pages.append(result)
                associated = related_to_paper(paper, result.related_paper_ids, result.title)
                children = result.links
                canonical = result.final_url
                steps.append(StepOutcome(step=step, status="completed", resource_url=link.url))
            visited.add(resource_key(link.model_copy(update={"url": canonical})))
            if associated:
                resources[key] = resources[key].model_copy(update={"relation": "related"})
            for child in children:
                if not admissible_child(child):
                    continue
                if associated and any(
                    re.search(r"\b(code|project|dataset|model|demo)\b", evidence.anchor, re.I)
                    for evidence in child.evidence
                ):
                    child = child.model_copy(update={"relation": "related"})
                add(child, depth + 1)
        except PwpError as exc:
            steps.append(
                StepOutcome(step=step, status="failed", resource_url=link.url, issue=exc.issue)
            )
            if exc.issue.code == "request_limit":
                limited = True
                break
        reporter.emit("step_progress", completed=len(pages) + len(repositories), unit="resources")
    if limited:
        steps.append(
            StepOutcome(step="discover_resources", status="partial", reason="limit_reached")
        )
    reporter.emit(
        "step_completed",
        paper_id=paper.arxiv_id,
        completed=len(pages) + len(repositories),
        unit="resources",
    )
    return DiscoveryResult(
        resources=list(resources.values()),
        pages=pages,
        repositories=repositories,
        steps=steps,
        limit_reached=limited,
    )
