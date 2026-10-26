"""Inspect public repository metadata and README links through the GitHub API."""

import asyncio
import base64
import binascii
import json
import re
from urllib.parse import quote, urlsplit

from markdown_it import MarkdownIt

from pwp.errors import PwpError, RequestError
from pwp.extract import merge_links
from pwp.extract.links import extract_text_links
from pwp.models import Issue, Repository
from pwp.network import SafeFetcher, require_success
from pwp.page import contextual_link, linked_paper_ids, parse_project_page


def repository_name(url: str) -> str:
    parts = urlsplit(url)
    segments = parts.path.strip("/").split("/")
    if parts.hostname not in {"github.com", "www.github.com"} or len(segments) < 2:
        raise ValueError("Expected a GitHub repository URL")
    owner, name = segments[:2]
    name = name.removesuffix(".git")
    if owner in {".", ".."} or name in {".", ".."}:
        raise ValueError("Invalid repository owner or name")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", owner) or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise ValueError("Invalid repository owner or name")
    return owner + "/" + name


TOKEN_HINT = "; configure a GitHub token to raise the unauthenticated limit of 60 requests/hour"
MAX_README_BYTES = 2 * 1024 * 1024


def _api_success(response, token: str | None) -> None:
    try:
        require_success(response)
    except RequestError as exc:
        if exc.issue.code != "rate_limited" or token:
            raise
        raise RequestError(
            "rate_limited",
            exc.issue.message + TOKEN_HINT,
            retryable=True,
            retry_after_s=exc.issue.retry_after_s,
            http_status=exc.issue.http_status,
        ) from exc


async def _read_readme(fetcher, name, branch, endpoint, headers, token):
    """Return README text and path, preferring raw content that spends no API quota."""
    raw_url = f"https://raw.githubusercontent.com/{name}/{quote(branch, safe='/')}/README.md"
    try:
        raw = await fetcher.get(raw_url, check_robots=False, max_bytes=MAX_README_BYTES)
        if raw.status == 200:
            return raw.body.decode("utf-8", errors="replace"), "README.md"
    except RequestError as exc:
        if exc.issue.code == "request_limit":
            raise
    # Other README names and locations need the API's discovery.
    readme = await fetcher.get(endpoint + "/readme", headers=headers, check_robots=False)
    _api_success(readme, token)
    payload = json.loads(readme.body)
    if not isinstance(payload, dict):
        raise ValueError("Expected a README object")
    if payload.get("encoding") != "base64":
        raise ValueError("Unsupported README encoding")
    text = base64.b64decode("".join(payload["content"].split()), validate=True).decode(
        "utf-8", errors="replace"
    )
    return text, payload.get("path") or "README.md"


async def inspect_repo(
    url: str, *, fetcher: SafeFetcher | None = None, token: str | None = None
) -> Repository:
    if fetcher is None:
        async with SafeFetcher() as owned:
            return await inspect_repo(url, fetcher=owned, token=token)
    try:
        name = repository_name(url)
    except ValueError as exc:
        raise RequestError("invalid_repository_url", str(exc)) from exc
    endpoint = f"https://api.github.com/repos/{name}"
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = "Bearer " + token
    response = await fetcher.get(endpoint, headers=headers, check_robots=False)
    _api_success(response, token)
    try:
        data = json.loads(response.body)
        canonical_name = repository_name("https://github.com/" + data["full_name"])
        basic = Repository(
            url="https://github.com/" + canonical_name,
            full_name=canonical_name,
            description=data.get("description") or "",
            homepage=data.get("homepage") or None,
            stars=data.get("stargazers_count"),
            pushed_at=data.get("pushed_at"),
            license=(data.get("license") or {}).get("spdx_id"),
        )
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise RequestError("invalid_repository", "Invalid GitHub repository response") from exc
    canonical = "https://github.com/" + canonical_name
    links = []
    homepage = data.get("homepage") or None
    if homepage:
        try:
            link = contextual_link(
                homepage,
                source="repository",
                source_url=canonical,
                anchor="Project homepage",
                context=data.get("description") or "",
            )
            if link:
                links.append(link)
        except ValueError:
            homepage = None
    description = data.get("description") or ""
    for link in extract_text_links(description, source="repository", source_url=canonical):
        converted = contextual_link(
            link.url, source="repository", source_url=canonical, anchor="", context=description
        )
        if converted:
            links.append(converted)
    issues = []
    readme_url = None
    try:
        default_branch = data.get("default_branch") or "main"
        text, readme_path = await _read_readme(
            fetcher, canonical_name, default_branch, endpoint, headers, token
        )
        branch = quote(default_branch, safe="")
        path = quote(readme_path, safe="/")
        readme_url = f"{canonical}/blob/{branch}/{path}"
        html = MarkdownIt("commonmark", {"html": True}).render(text)
        # README visuals are not used, and relative media would resolve to blob pages.
        page = await asyncio.to_thread(parse_project_page, html, readme_url, visuals=False)
        # Markdown destinations are read from parsed anchors, never a raw URL regex.
        for link in page.links:
            links.append(
                link.model_copy(
                    update={
                        "evidence": [
                            evidence.model_copy(update={"source": "repository"})
                            for evidence in link.evidence
                        ]
                    }
                )
            )
    except PwpError as exc:
        issues.append(exc.issue)
    except (ValueError, TypeError, KeyError, AttributeError, binascii.Error) as exc:
        issues.append(Issue(code="invalid_readme", message=str(exc)))
    links = merge_links(links)
    return basic.model_copy(
        update={
            "homepage": homepage,
            "readme_url": readme_url,
            "links": links,
            "related_paper_ids": linked_paper_ids(links),
            "issues": issues,
        }
    )
