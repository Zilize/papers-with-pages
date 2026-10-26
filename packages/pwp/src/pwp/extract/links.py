"""Find entry links without downloading full text or following URLs."""

import html
import re
from collections.abc import Iterable
from urllib.parse import urlsplit, urlunsplit

from pwp.models import LinkEvidence, Paper, ResourceLink

# Well-known resource hosts are often written without a scheme ("github.com/org/repo").
_BARE_HOSTS = ("github.com", "gitlab.com", "huggingface.co")
_URL = re.compile(
    r"(?:https?://|www\.|(?<![\w.@/-])(?:github\.com|gitlab\.com|huggingface\.co)/)"
    r"[^\s<>\"'`\\{}]+",
    re.IGNORECASE,
)
_GITHUB_RESERVED = {
    "about",
    "apps",
    "collections",
    "contact",
    "customer-stories",
    "enterprise",
    "events",
    "explore",
    "features",
    "login",
    "marketplace",
    "notifications",
    "orgs",
    "pricing",
    "readme",
    "search",
    "security",
    "settings",
    "site",
    "sponsors",
    "topics",
    "trending",
    "users",
}
_HUGGING_FACE_RESERVED = {
    "blog",
    "chat",
    "collections",
    "datasets",
    "docs",
    "join",
    "learn",
    "login",
    "organizations",
    "papers",
    "posts",
    "pricing",
    "settings",
    "spaces",
    "tasks",
}
# Code forges with a /owner/repository path layout.
_FORGES = {"gitee.com": "gitee", "codeberg.org": "codeberg", "bitbucket.org": "bitbucket"}
_FILE_HOSTS = {
    "drive.google.com",
    "docs.google.com",
    "dropbox.com",
    "onedrive.live.com",
    "1drv.ms",
    "box.com",
}


def project_page_exclusion(url: str) -> str | None:
    """Reject recognizable non-project destinations without fetching their content."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().removeprefix("www.")
    path = parts.path.lower()
    if host == "pypi.org" or host.endswith(".pypi.org") or host == "pypi.python.org":
        return "package_registry"
    if host == "anonymous.4open.science" or host.endswith(".anonymous.4open.science"):
        return "code_host"
    publication_hosts = {
        "arxiv.org",
        "openreview.net",
        "doi.org",
        "aclanthology.org",
        "proceedings.mlr.press",
        "openaccess.thecvf.com",
        "ieeexplore.ieee.org",
        "dl.acm.org",
        "link.springer.com",
        "sciencedirect.com",
        "nature.com",
        "science.org",
        "semanticscholar.org",
        "pubmed.ncbi.nlm.nih.gov",
    }
    if any(host == domain or host.endswith("." + domain) for domain in publication_hosts):
        return "publication"
    if host in {"twitter.com", "x.com", "linkedin.com", "scholar.google.com", "orcid.org"}:
        return "profile"
    if any(host == domain or host.endswith("." + domain) for domain in _FILE_HOSTS):
        return "file_host"
    if re.search(r"\.(pdf|ps|zip|gz|tar|png|jpe?g|gif|svg|webp|mp4|webm|bib|csv|json)$", path):
        return "file"
    segments = [segment for segment in path.split("/") if segment]
    if any(
        segment in {"docs", "documentation", "api", "reference", "tutorials"}
        for segment in segments
    ):
        return "documentation"
    if any(
        segment in {"people", "person", "members", "team", "profile", "publications", "cv"}
        for segment in segments
    ):
        return "profile"
    if host.endswith(".github.io") and not segments:
        return "ambiguous_homepage"
    return None


def is_telemetry_url(url: str) -> bool:
    """Identify background analytics and ads, not functional page dependencies."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().removeprefix("www.")
    if any(
        host == domain or host.endswith("." + domain)
        for domain in (
            "google-analytics.com",
            "doubleclick.net",
            "ethicalads.io",
        )
    ):
        return True
    if host == "google.com" and parts.path == "/g/collect":
        return True
    return host in {"youtube.com", "youtube-nocookie.com"} and (
        parts.path.startswith("/api/stats/") or parts.path == "/youtubei/v1/log_event"
    )


def _trim_url(value: str) -> str:
    value = re.split(r"\)\s*\[|\]\s*\[|\]\s*\(", value, maxsplit=1)[0]
    value = value.rstrip(".,;:!?")
    for opening, closing in [("(", ")"), ("[", "]"), ("{", "}")]:
        while value.endswith(closing) and value.count(closing) > value.count(opening):
            value = value[:-1].rstrip(".,;:!?")
    return value


def normalize_url(value: str) -> str:
    """Preserve paths, query parameters and fragments; reject credential URLs."""
    value = re.sub(
        r"&(?:#[0-9]+|#x[0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]+);",
        lambda match: html.unescape(match.group()),
        value.strip(),
    )
    lowered = value.lower()
    if lowered.startswith("www.") or lowered.startswith(tuple(host + "/" for host in _BARE_HOSTS)):
        value = "https://" + value
    if any(char.isspace() or ord(char) < 32 for char in value) or "\\" in value:
        raise ValueError("URLs must not contain whitespace or backslashes")
    parts = urlsplit(value)
    if parts.scheme.lower() not in {"https", "http"} or not parts.hostname:
        raise ValueError("Expected an absolute HTTP(S) URL")
    if parts.username is not None or parts.password is not None:
        raise ValueError("Credential-bearing URLs are not accepted")
    host = parts.hostname.encode("idna").decode("ascii").lower()
    if ":" in host:
        host = f"[{host}]"
    port = parts.port
    scheme = parts.scheme.lower()
    if port is not None and (scheme, port) not in {("http", 80), ("https", 443)}:
        host += f":{port}"
    return urlunsplit((scheme, host, parts.path or "/", parts.query, parts.fragment))


def classify_link(value: str) -> ResourceLink:
    """Classify known URL shapes without claiming association with a paper."""
    url = normalize_url(value)
    parts = urlsplit(url)
    host = parts.hostname or ""
    path = [part for part in parts.path.split("/") if part]
    kind = "other"
    platform = None
    bare_host = host.removeprefix("www.")
    if host in {"github.com", "www.github.com"}:
        platform = "github"
        if len(path) >= 2 and path[0].lower() not in _GITHUB_RESERVED:
            kind = "repository"
    elif bare_host in _FORGES:
        platform = _FORGES[bare_host]
        if len(path) >= 2:
            kind = "repository"
    elif host == "anonymous.4open.science" or host.endswith(".anonymous.4open.science"):
        platform = "anonymous_4open"
        if len(path) >= 2 and path[0].lower() == "r":
            kind = "repository"
    elif host.endswith(".github.io"):
        platform = "github_pages"
        if path:
            kind = "project_page"
    elif host == "gitlab.com" or host.startswith(("gitlab.", "git.")):
        platform = "gitlab"
        if len(path) >= 2:
            kind = "repository"
    elif host in {"huggingface.co", "www.huggingface.co"}:
        platform = "hugging_face"
        if len(path) >= 3 and path[0] == "datasets":
            kind = "dataset"
        elif len(path) >= 3 and path[0] == "spaces":
            kind = "demo"
        elif len(path) >= 2 and path[0] == "papers":
            kind = "paper"
        elif len(path) >= 2 and path[0] not in _HUGGING_FACE_RESERVED:
            kind = "model"
    elif bare_host in {"modelscope.cn", "modelscope.ai"}:
        platform = "modelscope"
        section = path[0].lower() if path else ""
        if len(path) >= 3:
            kind = {"models": "model", "datasets": "dataset", "studios": "demo"}.get(
                section, "other"
            )
    elif bare_host == "kaggle.com":
        platform = "kaggle"
        if len(path) >= 3 and path[0] in {"datasets", "models"}:
            kind = "dataset" if path[0] == "datasets" else "model"
    elif bare_host == "zenodo.org" or bare_host.endswith(".zenodo.org"):
        platform = "zenodo"
        if len(path) >= 2 and path[0] in {"record", "records", "doi"}:
            kind = "dataset"
    elif host == "colab.research.google.com":
        platform = "colab"
        if path:
            kind = "demo"
    elif host in {"youtube.com", "www.youtube.com", "youtu.be", "vimeo.com", "www.vimeo.com"}:
        platform = "youtube" if "youtu" in host else "vimeo"
        if path and (host == "youtu.be" or path[0] in {"watch", "embed", "shorts"}):
            kind = "video"
        elif platform == "vimeo" and path and path[0].isdigit():
            kind = "video"
    elif host in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}:
        platform = "arxiv"
        if len(path) >= 2 and path[0] in {"abs", "pdf", "html"}:
            kind = "paper"
    if project_page_exclusion(url) == "publication":
        kind = "paper"
    elif kind == "project_page" and project_page_exclusion(url):
        kind = "other"
    return ResourceLink(url=url, kind=kind, platform=platform)


def merge_links(links: Iterable[ResourceLink]) -> list[ResourceLink]:
    """Merge identical normalized URLs while retaining every distinct evidence item."""
    merged: dict[str, ResourceLink] = {}
    for link in links:
        key = normalize_url(link.url)
        if key not in merged:
            merged[key] = link.model_copy(update={"url": key})
            continue
        previous = merged[key]
        evidence = list(previous.evidence)
        for item in link.evidence:
            if item not in evidence:
                evidence.append(item)
        merged[key] = previous.model_copy(update={"evidence": evidence})
    return list(merged.values())


def extract_metadata_links(paper: Paper) -> list[ResourceLink]:
    """Extract links from abstract and comments, returning an empty list if absent."""
    links: list[ResourceLink] = []
    for source, text in [("abstract", paper.abstract), ("comments", paper.comments or "")]:
        links.extend(extract_text_links(text, source=source))
    return merge_links(links)


def extract_text_links(
    text: str, *, source: str, source_url: str | None = None
) -> list[ResourceLink]:
    """Extract explicit text URLs while preserving the caller's provenance."""
    text = re.sub(r"(?<=\))(?=\[)", " ", text)
    text = re.sub(r"\.(?=(?:website|code|project(?: page)?|demo):https?://)", " ", text, flags=re.I)
    links = []
    for match in _URL.finditer(text):
        raw = _trim_url(match.group())
        try:
            link = classify_link(raw)
        except ValueError:
            continue
        context = text[max(0, match.start() - 100) : min(len(text), match.end() + 100)]
        links.append(
            link.model_copy(
                update={
                    "evidence": [
                        LinkEvidence(
                            original_url=raw, source=source, source_url=source_url, context=context
                        )
                    ]
                }
            )
        )
    return merge_links(links)
