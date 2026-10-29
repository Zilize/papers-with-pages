"""Parse existing project HTML; parsing never performs network requests."""

import asyncio
import re
from collections.abc import Iterable
from urllib.parse import urljoin, urlsplit

import bibtexparser
from bs4 import BeautifulSoup

from pwp.affiliations import parse_author_block
from pwp.errors import RequestError
from pwp.extract import classify_link, merge_links, normalize_url
from pwp.extract.links import project_page_exclusion
from pwp.models import Citation, Issue, LinkEvidence, ProjectPage, ResourceLink
from pwp.network import SafeFetcher, require_success
from pwp.patterns import NOISE, action_label
from pwp.refs import parse_paper_ref

# Parents with more links than this are usually navigation or reference lists.
CROWDED_LINK_GROUP = 8
_HF_PAPER = re.compile(r"^/papers/(?P<id>\d{4}\.\d{4,5})(?:v\d+)?/?$")

_NEGATIVE = re.compile(r"\b(template|website source|personal|homepage of|twitter|linkedin)\b", re.I)
_PROJECT = re.compile(r"\b(project(?: page)?|website|homepage)\b", re.I)
_ROOT_PAGE_CONTEXT = re.compile(
    r"\b(project(?: page)?|website|web ?page|homepage|demos?|videos?|results|supplementary|"
    r"material|code|open-sourced?|released)\b",
    re.I,
)
_RESOURCE = re.compile(r"\b(code|repository|github|dataset|model|demo|project|website)\b", re.I)


def absolute_url(value: str, base: str) -> str | None:
    if not value or value.strip().startswith(("#", "javascript:", "data:", "mailto:", "tel:")):
        return None
    try:
        return normalize_url(urljoin(base, value.strip()))
    except ValueError:
        return None


def contextual_link(url: str, *, source: str, source_url: str, anchor: str, context=""):
    link = classify_link(url)
    description = anchor or context
    if _NEGATIVE.search(description):
        return None
    if (
        link.kind == "other"
        and link.platform in {None, "github_pages"}
        and _PROJECT.search(description)
        and project_page_exclusion(url) in {None, "ambiguous_homepage"}
    ):
        link = link.model_copy(update={"kind": "project_page"})
    return link.model_copy(
        update={
            "evidence": [
                LinkEvidence(
                    original_url=url,
                    source=source,
                    source_url=source_url,
                    anchor=anchor,
                    context=context[:500],
                )
            ]
        }
    )


def paper_id_from_url(url: str) -> str | None:
    """Return the arXiv ID named by an arXiv or Hugging Face paper URL."""
    parts = urlsplit(url)
    if (parts.hostname or "").removeprefix("www.") == "huggingface.co":
        match = _HF_PAPER.match(parts.path)
        return match["id"] if match else None
    try:
        return parse_paper_ref(url).arxiv_id
    except ValueError:
        return None


def linked_paper_ids(links: Iterable[ResourceLink]) -> list[str]:
    """Paper IDs in order of first appearance."""
    ids = []
    for link in links:
        if link.kind == "paper":
            identifier = paper_id_from_url(link.url)
            if identifier and identifier not in ids:
                ids.append(identifier)
    return ids


def parse_bibtex(raw: str) -> Citation:
    """Keep the original citation even when structured parsing fails."""
    if len(raw) > 100_000:
        return Citation(
            raw=raw, issue=Issue(code="citation_too_large", message="Citation is too large")
        )
    try:
        database = bibtexparser.loads(raw)
        entries = [{str(k): str(v) for k, v in item.items()} for item in database.entries]
        if not entries:
            raise ValueError("No BibTeX entries found")
        return Citation(raw=raw, entries=entries)
    except Exception as exc:
        return Citation(raw=raw, issue=Issue(code="invalid_bibtex", message=str(exc)))


def parse_project_page(
    html: str,
    url: str,
    *,
    original_url: str | None = None,
    http_status: int = 200,
    observed_media: Iterable[str] = (),
    visuals: bool = True,
) -> ProjectPage:
    """Extract page facts and resource evidence without assuming paper ownership.

    This is CPU-bound; async callers should run it in a worker thread. Pass
    ``visuals=False`` when the page cannot provide main visuals or a title area with
    author declarations (for example READMEs).
    """
    from pwp.visuals import discover_visuals

    soup = BeautifulSoup(html, "html.parser")
    base_node = soup.find("base", href=True)
    base = absolute_url(base_node["href"], url) if base_node else url
    base = base or url
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    description_node = soup.find("meta", attrs={"name": "description"}) or soup.find(
        "meta", attrs={"property": "og:description"}
    )
    description = description_node.get("content", "") if description_node else ""
    canonical_node = soup.find("link", rel="canonical")
    canonical = absolute_url(canonical_node.get("href", ""), base) if canonical_node else None
    links = []
    section = ""
    group_sizes: dict[int, int] = {}
    # One document-order pass tracks the nearest preceding heading in linear time.
    for node in soup.find_all(["h1", "h2", "h3", "h4", "a"]):
        if node.name != "a":
            section = node.get_text(" ", strip=True)
            continue
        if not node.has_attr("href"):
            continue
        target = absolute_url(node["href"], base)
        if target is None or NOISE.search(section) or node.find_parent("footer"):
            continue
        anchor = (
            node.get_text(" ", strip=True) or node.get("title", "") or node.get("aria-label", "")
        )
        if not anchor:
            img = node.find("img", alt=True)
            anchor = img["alt"] if img else ""
        parent = node.parent
        size = group_sizes.get(id(parent))
        if size is None:
            size = group_sizes[id(parent)] = len(parent.find_all("a", href=True))
        # Crowded groups are navigation or reference lists, except explicit resource
        # buttons, which project pages often place in one long row.
        if size > CROWDED_LINK_GROUP and not action_label(anchor):
            continue
        context = (
            parent.get_text(" ", strip=True)[:500] if parent.name in {"p", "li", "td"} else anchor
        )
        context = f"{section}: {context}"
        link = contextual_link(
            target, source="project_page", source_url=url, anchor=anchor, context=context
        )
        if link:
            links.append(link)
    links = merge_links(links)
    citations = []
    seen = set()
    for node in soup.select("pre, code, textarea, script[type='application/x-bibtex']"):
        raw = node.get_text("\n", strip=True)
        if re.search(r"@\w+\s*[{(]", raw) and raw not in seen:
            seen.add(raw)
            citations.append(parse_bibtex(raw))
    authors = [node.get("content", "") for node in soup.select("meta[name='citation_author']")]
    institutions = [
        node.get("content", "") for node in soup.select("meta[name='citation_author_institution']")
    ]
    return ProjectPage(
        url=original_url or url,
        final_url=url,
        http_status=http_status,
        title=title,
        description=description,
        canonical_url=canonical,
        links=links,
        visuals=discover_visuals(html, url, observed_media=observed_media, soup=soup)
        if visuals
        else [],
        citations=citations,
        authors=list(dict.fromkeys(filter(None, authors))),
        institutions=list(dict.fromkeys(filter(None, institutions))),
        author_block=parse_author_block(soup) if visuals else None,
        related_paper_ids=linked_paper_ids(links),
    )


async def inspect_page(url: str, *, fetcher: SafeFetcher | None = None) -> ProjectPage:
    if fetcher is None:
        async with SafeFetcher() as owned:
            return await inspect_page(url, fetcher=owned)
    response = await fetcher.get(url)
    require_success(response)
    content_type = response.headers.get("content-type", "").lower()
    if "html" not in content_type and not response.body.lstrip().lower().startswith(
        (b"<!doctype html", b"<html")
    ):
        raise RequestError("not_html", "The resource is not an HTML project page")
    return await asyncio.to_thread(
        parse_project_page,
        response.text,
        response.final_url,
        original_url=url,
        http_status=response.status,
    )


def can_explore(link: ResourceLink) -> bool:
    if link.kind == "repository" and link.platform not in {None, "github"}:
        return False
    if link.kind not in {"repository", "project_page"}:
        return False
    if any(item.source in {"abstract", "comments"} for item in link.evidence):
        return True
    return any(_RESOURCE.search(item.anchor) for item in link.evidence)


def seed_link(link: ResourceLink) -> ResourceLink:
    """Allow unknown metadata URLs as candidates, excluding obvious profile/file links."""
    if link.kind != "other" or link.platform not in {None, "github_pages"}:
        return link
    exclusion = project_page_exclusion(link.url)
    # Metadata often introduces a root *.github.io project page as code, results, or
    # supplementary material rather than as a "project page"; bare mentions stay
    # ambiguous because they may be personal homepages.
    if exclusion and not (
        exclusion == "ambiguous_homepage"
        and any(_ROOT_PAGE_CONTEXT.search(item.context) for item in link.evidence)
    ):
        return link
    if any(_NEGATIVE.search(item.context) for item in link.evidence):
        return link
    parts = urlsplit(link.url)
    host = parts.hostname or ""
    blocked = {"twitter.com", "x.com", "linkedin.com", "scholar.google.com", "orcid.org"}
    if host.removeprefix("www.") in blocked or re.search(
        r"\.(pdf|zip|gz|png|jpg|mp4)$", parts.path, re.I
    ):
        return link
    if any(item.source in {"abstract", "comments"} for item in link.evidence):
        return link.model_copy(update={"kind": "project_page"})
    return link
