"""Compose library operations without owning persistent jobs or result storage."""

import asyncio
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from pwp.browser import RenderSession
from pwp.capture import render_page
from pwp.discover import discover_resources
from pwp.errors import PwpError
from pwp.events import EventCallbackError, EventHandler, EventReporter
from pwp.harvest import ArxivClient
from pwp.link_policy import apply_page_availability, terminal_page_issue
from pwp.media import download_image
from pwp.models import DiscoveryLimits, Issue, Paper, PaperResult, ProjectPage, StepOutcome
from pwp.network import RequestBudget, SafeFetcher, request_scope
from pwp.page import inspect_page


@dataclass(frozen=True)
class _ResolveOptions:
    work_dir: Path | None
    render: bool
    download_images: bool
    max_pages: int
    max_images: int
    limits: DiscoveryLimits
    fetcher: SafeFetcher
    github_token: str | None
    on_event: EventHandler | None
    render_session: RenderSession


def _validate_options(work_dir, render, download_images, max_pages, max_images) -> None:
    if (render or download_images) and work_dir is None:
        raise ValueError("A work_dir is required for rendering or downloading images")
    if max_pages < 1 or max_images < 0:
        raise ValueError("Invalid media limits")


async def resolve(
    paper: Paper | str,
    *,
    work_dir: str | Path | None = None,
    render: bool = True,
    download_images: bool = False,
    max_pages: int = 3,
    max_images: int = 1,
    limits: DiscoveryLimits | None = None,
    metadata_client: ArxivClient | None = None,
    fetcher: SafeFetcher | None = None,
    trusted_proxy: str | None = None,
    render_session: RenderSession | None = None,
    github_token: str | None = None,
    on_event: EventHandler | None = None,
) -> PaperResult:
    """Return partial results; output files exist only in an explicit work directory."""
    _validate_options(work_dir, render, download_images, max_pages, max_images)
    if fetcher is not None and trusted_proxy is not None:
        raise ValueError("Configure trusted_proxy on the supplied fetcher instead")
    if render_session is None:
        async with RenderSession() as session:
            return await resolve(
                paper,
                work_dir=work_dir,
                render=render,
                download_images=download_images,
                max_pages=max_pages,
                max_images=max_images,
                limits=limits,
                metadata_client=metadata_client,
                fetcher=fetcher,
                trusted_proxy=trusted_proxy,
                github_token=github_token,
                on_event=on_event,
                render_session=session,
            )
    requested = paper.identifier if isinstance(paper, Paper) else paper
    reporter = EventReporter("resolve", on_event)
    reporter.emit("step_started", paper_id=requested)
    steps = []
    if not isinstance(paper, Paper):
        try:
            if metadata_client is None:
                async with ArxivClient() as owned:
                    paper = await owned.get_paper(paper, on_event=on_event)
            else:
                paper = await metadata_client.get_paper(paper, on_event=on_event)
        except PwpError as exc:
            reporter.emit("step_failed", issue=exc.issue)
            return PaperResult(
                requested=requested,
                status="failed",
                steps=[StepOutcome(step="get_paper", status="failed", issue=exc.issue)],
            )
    steps.append(StepOutcome(step="get_paper", status="completed"))
    limits = limits or DiscoveryLimits()

    def options(active_fetcher: SafeFetcher) -> _ResolveOptions:
        return _ResolveOptions(
            work_dir=Path(work_dir) if work_dir is not None else None,
            render=render,
            download_images=download_images,
            max_pages=max_pages,
            max_images=max_images,
            limits=limits,
            fetcher=active_fetcher,
            github_token=github_token,
            on_event=on_event,
            render_session=render_session,
        )

    if fetcher is None:
        async with SafeFetcher(
            budget=RequestBudget(limits.max_requests), trusted_proxy=trusted_proxy
        ) as owned:
            result = await _resolve_resources(paper, requested, steps, options(owned))
    else:
        result = await _resolve_resources(paper, requested, steps, options(fetcher))
    reporter.emit("step_completed", paper_id=paper.arxiv_id)
    return result


async def _resolve_resources(
    paper: Paper, requested: str, steps: list[StepOutcome], options: _ResolveOptions
) -> PaperResult:
    with request_scope(RequestBudget(options.limits.max_requests)):
        return await _resolve_resources_scoped(paper, requested, steps, options)


async def _resolve_resources_scoped(
    paper: Paper, requested: str, steps: list[StepOutcome], options: _ResolveOptions
) -> PaperResult:
    media = []
    # Each invocation owns only its unique scratch directory, never the parent's lifecycle.
    output = options.work_dir / uuid4().hex if options.work_dir is not None else None
    rendered_count = 0

    async def load_page(url: str) -> ProjectPage:
        nonlocal rendered_count
        if options.render and rendered_count < options.max_pages:
            rendered_count += 1
            try:
                rendered = await render_page(
                    url,
                    work_dir=output,
                    fetcher=options.fetcher,
                    on_event=options.on_event,
                    render_session=options.render_session,
                )
                if rendered.screenshot:
                    media.append(rendered.screenshot)
                steps.append(
                    StepOutcome(
                        step="render_page",
                        resource_url=url,
                        status="partial"
                        if rendered.issues and not rendered.screenshot
                        else "completed",
                        issue=next(
                            (i for i in rendered.issues if i.code == "capture_failed"),
                            rendered.issues[0] if rendered.issues else None,
                        ),
                        issues=rendered.issues,
                    )
                )
                return rendered.page
            except PwpError as exc:
                steps.append(
                    StepOutcome(
                        step="render_page",
                        status="failed",
                        resource_url=url,
                        issue=exc.issue,
                    )
                )
                if terminal_page_issue(exc.issue.code, exc.issue.message, exc.issue.http_status):
                    raise
        elif options.render:
            steps.append(
                StepOutcome(
                    step="render_page", status="skipped", resource_url=url, reason="page_limit"
                )
            )
        return await inspect_page(url, fetcher=options.fetcher)

    discovered = await discover_resources(
        paper,
        limits=options.limits,
        fetcher=options.fetcher,
        github_token=options.github_token,
        on_event=options.on_event,
        page_loader=load_page,
    )
    steps.extend(discovered.steps)
    pages = list(discovered.pages)
    if options.render and not rendered_count:
        steps.append(StepOutcome(step="render_page", status="skipped", reason="no_project_page"))
    if not options.render:
        steps.append(StepOutcome(step="render_page", status="skipped", reason="disabled"))
    visuals = []
    seen = set()
    for page in pages:
        for candidate in page.visuals:
            if candidate.id not in seen:
                visuals.append(candidate)
                seen.add(candidate.id)
    if options.download_images:
        images = [
            item for item in visuals if item.media_type == "image" and item.delivery == "direct"
        ]
        for candidate in images[: options.max_images]:
            try:
                media.append(
                    await download_image(candidate, work_dir=output, fetcher=options.fetcher)
                )
                steps.append(
                    StepOutcome(
                        step="download_image", status="completed", resource_url=candidate.url
                    )
                )
            except PwpError as exc:
                steps.append(
                    StepOutcome(
                        step="download_image",
                        status="failed",
                        resource_url=candidate.url,
                        issue=exc.issue,
                    )
                )
    status = (
        "partial" if any(step.status in {"failed", "partial"} for step in steps) else "completed"
    )
    return PaperResult(
        requested=requested,
        paper=paper,
        resources=apply_page_availability(
            discovered.resources,
            [step.model_dump(mode="json") for step in steps],
            [page.model_dump(mode="json") for page in pages],
        ),
        pages=pages,
        repositories=discovered.repositories,
        visuals=visuals,
        media=media,
        steps=steps,
        status=status,
    )


async def _resolve_isolated(item: Paper | str, kwargs: dict) -> PaperResult:
    """Turn one paper's unexpected failure into its result instead of ending the batch.

    Cancellation and caller callback errors still propagate; shared arguments are
    validated before scheduling, so remaining errors belong to this paper.
    """
    try:
        return await resolve(item, **kwargs)
    except EventCallbackError:
        raise
    except Exception as exc:
        return PaperResult(
            requested=item.identifier if isinstance(item, Paper) else str(item),
            paper=item if isinstance(item, Paper) else None,
            status="failed",
            steps=[
                StepOutcome(
                    step="resolve",
                    status="failed",
                    issue=Issue(
                        code="internal_error",
                        message=f"{type(exc).__name__}: {exc}"[:500],
                    ),
                )
            ],
        )


async def resolve_many(
    papers: Iterable[Paper | str], *, concurrency: int = 2, **kwargs
) -> AsyncIterator[PaperResult]:
    """Yield in completion order with bounded in-flight work and no hidden task database.

    A paper that fails unexpectedly yields a failed result; the batch continues.
    """
    if isinstance(papers, (str, Paper)):
        raise TypeError("Pass an iterable of papers")
    if not 1 <= concurrency <= 16:
        raise ValueError("concurrency must be between 1 and 16")
    _validate_options(
        kwargs.get("work_dir"),
        kwargs.get("render", True),
        kwargs.get("download_images", False),
        kwargs.get("max_pages", 3),
        kwargs.get("max_images", 1),
    )
    owned = kwargs.get("metadata_client") is None
    client = kwargs.get("metadata_client") or ArxivClient()
    owns_fetcher = kwargs.get("fetcher") is None
    trusted_proxy = kwargs.pop("trusted_proxy", None)
    if not owns_fetcher and trusted_proxy is not None:
        raise ValueError("Configure trusted_proxy on the supplied fetcher instead")
    fetcher = kwargs.get("fetcher") or SafeFetcher(
        budget=RequestBudget(maximum=None), trusted_proxy=trusted_proxy
    )
    kwargs = {**kwargs, "metadata_client": client, "fetcher": fetcher}
    owns_renderer = kwargs.get("render_session") is None
    renderer = kwargs.get("render_session") or RenderSession()
    kwargs["render_session"] = renderer
    iterator = iter(papers)
    tasks = set()

    def schedule():
        try:
            item = next(iterator)
        except StopIteration:
            return False
        tasks.add(asyncio.create_task(_resolve_isolated(item, kwargs)))
        return True

    try:
        for _ in range(concurrency):
            if not schedule():
                break
        while tasks:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            tasks = pending
            # Consume every done task before yielding so cleanup also handles simultaneous failures.
            results = []
            for task in done:
                try:
                    results.append(task.result())
                except BaseException:
                    for completed in done:
                        if completed is not task and not completed.cancelled():
                            completed.exception()
                    raise
            for result in results:
                yield result
                schedule()
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if owns_renderer:
            await renderer.aclose()
        if owned:
            await client.aclose()
        if owns_fetcher:
            await fetcher.aclose()
