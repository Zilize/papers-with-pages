"""Conservative resource admission, shared by discovery and saved-result views."""

from urllib.parse import urlsplit

from pwp.extract.links import classify_link, extract_metadata_links, project_page_exclusion
from pwp.patterns import ACTION, NOISE, action_label

__all__ = [
    "ACTION",
    "NOISE",
    "admissible_child",
    "apply_page_availability",
    "identity",
    "select_resources",
    "terminal_page_issue",
    "unavailable_project_urls",
]


def identity(url):
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    path = parts.path.rstrip("/")
    if host in {"github.com", "www.github.com"}:
        path = "/" + "/".join(path.strip("/").split("/")[:2]).removesuffix(".git")
        path = path.lower()
    return host + path


TERMINAL_CODES = {
    "robots_denied",
    "blocked_url",
    "blocked_dns",
    "not_html",
    "document_blocked",
    "redirect_loop",
    "redirect_limit",
}
TERMINAL_HTTP_STATUSES = {401, 403, 404, 410, 451}


def terminal_page_issue(code: str, message: str = "", http_status: int | None = None) -> bool:
    """Distinguish unavailable main documents from transient or asset failures.

    Saved results from before ``Issue.http_status`` existed only carry the message.
    """
    if code in TERMINAL_CODES:
        return True
    if code != "http_error":
        return False
    if http_status is None:
        status = message.removeprefix("HTTP ")
        http_status = int(status) if status.isdigit() else None
    return http_status in TERMINAL_HTTP_STATUSES


def unavailable_project_urls(steps, pages=()):
    """Derive availability from saved facts, keeping the original diagnostics."""
    available = {
        identity(url)
        for page in pages
        if 200 <= page.get("http_status", 200) < 300
        for url in (page["url"], page["final_url"])
    }
    unavailable = set()
    for step in steps:
        url = step.get("resource_url")
        if not url or step["step"] not in {"render_page", "inspect_page"}:
            continue
        if step.get("status") != "failed":
            continue
        for issue in step.get("issues") or [step.get("issue")]:
            if not issue or (
                issue.get("resource_url") and identity(issue["resource_url"]) != identity(url)
            ):
                continue
            if terminal_page_issue(issue["code"], issue["message"], issue.get("http_status")):
                unavailable.add(identity(url))
    return unavailable - available


def apply_page_availability(resources, steps, pages=()):
    unavailable = unavailable_project_urls(steps, pages)
    return [
        link.model_copy(update={"kind": "other"})
        if link.kind == "project_page" and identity(link.url) in unavailable
        else link
        for link in resources
    ]


def admissible_child(link):
    if any(token in link.url for token in ("](", "]][", "}{")):
        return False
    if link.kind not in {
        "repository",
        "project_page",
        "dataset",
        "model",
        "demo",
        "video",
        "paper",
    }:
        return False
    if link.kind == "project_page" and project_page_exclusion(link.url) not in {
        None,
        "ambiguous_homepage",
    }:
        return False
    for evidence in link.evidence:
        if evidence.source == "project_page" and link.kind == "project_page":
            if not evidence.source_url or identity(evidence.source_url) != identity(link.url):
                continue
        if evidence.source == "repository" and link.kind == "repository":
            if not evidence.source_url or identity(evidence.source_url) != identity(link.url):
                continue
        if not NOISE.search(evidence.context + " " + evidence.anchor) and action_label(
            evidence.anchor
        ):
            return True
    return False


def select_resources(paper, resources):
    """Keep metadata roots and explicit resource buttons reachable from those roots."""
    roots = {identity(link.url) for link in extract_metadata_links(paper)}
    selected = []
    remaining = list(resources)
    for _ in range(len(remaining) + 1):
        added = []
        for link in remaining:
            key = identity(link.url)
            direct = key in roots
            reachable = any(e.source_url and identity(e.source_url) in roots for e in link.evidence)
            if not direct and not (reachable and admissible_child(link)):
                continue
            if any(token in link.url for token in ("](", "]][", "}{")):
                continue
            if link.kind == "project_page":
                classified = classify_link(link.url)
                if classified.kind == "repository" or project_page_exclusion(link.url) not in {
                    None,
                    "ambiguous_homepage",
                }:
                    link = classified.model_copy(update={"evidence": link.evidence})
            if not any(identity(item.url) == key for item in selected):
                selected.append(link)
            added.append(key)
        if not added:
            break
        roots.update(added)
        remaining = [link for link in remaining if identity(link.url) not in added]
    return selected
