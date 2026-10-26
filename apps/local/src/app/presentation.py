"""Choose paper-owned resources, hero covers, videos, and actionable messages."""

from pwp.extract.links import is_telemetry_url
from pwp.link_policy import apply_page_availability, identity, select_resources
from pwp.models import Paper, ResourceLink, VisualCandidate
from pwp.visuals import select_main_video


def prepare(paper, result):
    if not result:
        return {"cover": None, "videos": [], "problems": [], "warnings": []}
    links = select_resources(
        Paper.model_validate(paper), [ResourceLink.model_validate(r) for r in result["resources"]]
    )
    links = apply_page_availability(links, result["steps"], result.get("pages", []))
    result["resources"] = [link.model_dump(mode="json") for link in links]
    pages = [link for link in links if link.kind == "project_page"]
    main = identity(pages[0].url) if pages else None
    aliases = {main} if main else set()
    for page in result.get("pages", []):
        if identity(page["url"]) == main:
            aliases.add(identity(page["final_url"]))
    heroes = [
        m
        for m in result["media"]
        if m["kind"] == "hero_screenshot" and identity(m["source_url"]) in aliases
    ]
    main_video = select_main_video(
        (
            VisualCandidate.model_validate(visual)
            for visual in result["visuals"]
            if visual["media_type"] == "video" and identity(visual["source_page_url"]) in aliases
        ),
        allow_player_fallback=True,
    )
    videos = [main_video.model_dump(mode="json")] if main_video else []
    problems, warnings = [], []
    retained = {identity(link.url) for link in links if link.kind != "other"}
    expanded = [
        dict(step, issue=issue)
        for step in result["steps"]
        for issue in (step.get("issues") or [step.get("issue")])
    ]
    for step in expanded:
        issue = step.get("issue")
        if not issue and step.get("reason") == "limit_reached":
            problems.append(
                {
                    "message": "Link discovery reached its exploration limit",
                    "url": step.get("resource_url"),
                    "step": step["step"],
                }
            )
        if not issue:
            continue
        if issue.get("resource_url") and is_telemetry_url(issue["resource_url"]):
            continue
        if step.get("resource_url") and identity(step["resource_url"]) not in retained:
            continue
        message = {
            "robots_denied": "This resource disallows automated access (robots.txt)",
            "blocked_dns": "Resource blocked by DNS policy",
            "rate_limited": "Upstream request limit reached",
            "robots_unavailable": "Could not read robots policy",
            "asset_timeout": "Some page assets timed out",
            "method_blocked": "A page background request was blocked",
            "backoff_active": "The server requested a longer wait",
        }.get(issue["code"], issue["message"])
        if step["step"] == "inspect_repo" and issue["code"] == "http_error":
            message = f"Repository access failed ({issue['message']})"
        if issue["code"] == "rate_limited" and step["step"] == "inspect_repo":
            message = "GitHub request limit reached"
        if issue.get("retry_after_s"):
            message += f" (retry in {int(issue['retry_after_s']) + 1}s)"
        if (
            step["step"] == "render_page"
            and issue.get("resource_url")
            and identity(issue["resource_url"]) != identity(step.get("resource_url") or "")
        ):
            message = "Page asset: " + message
        entry = {
            "code": issue["code"],
            "message": message,
            "url": issue.get("resource_url") or step.get("resource_url"),
            "step": step["step"],
        }
        supplementary = step["step"] == "render_page" and any(
            identity(m["source_url"]) == identity(step.get("resource_url") or "")
            for m in result["media"]
            if m["kind"] == "hero_screenshot"
        )
        target = warnings if supplementary else problems
        if issue["code"] == "capture_failed":
            target.insert(0, entry)
        else:
            target.append(entry)
    return {
        "quality": "needs_attention" if problems else "with_warnings" if warnings else "complete",
        "cover": "/assets/" + heroes[0]["path"] if heroes else None,
        "videos": videos,
        "problems": problems,
        "warnings": warnings,
    }
