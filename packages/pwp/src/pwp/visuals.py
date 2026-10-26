"""Discover and rank main visuals from existing HTML and observed media URLs."""

import hashlib
import re
from collections.abc import Iterable
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup

from pwp.models import VisualCandidate, VisualSource
from pwp.page import absolute_url


def _integer(value):
    try:
        number = int(value)
        return number if number > 0 else None
    except (ValueError, TypeError):
        return None


def video_identity(url):
    parts = urlsplit(url)
    host = (parts.hostname or "").removeprefix("www.")
    segments = parts.path.strip("/").split("/")
    identifier = None
    provider = None
    if host in {"youtube.com", "youtube-nocookie.com", "youtu.be"}:
        provider = "youtube"
        if host == "youtu.be":
            identifier = segments[0]
        elif segments[0] in {"embed", "shorts"} and len(segments) > 1:
            identifier = segments[1]
        else:
            identifier = parse_qs(parts.query).get("v", [None])[0]
        if identifier and not re.fullmatch(r"[A-Za-z0-9_-]{6,64}", identifier):
            identifier = None
    elif host in {"vimeo.com", "player.vimeo.com"}:
        provider = "vimeo"
        identifier = next((part for part in reversed(segments) if part.isdigit()), None)
    return (provider, identifier) if identifier else (None, None)


_SECONDARY_SECTION = re.compile(
    r"result|compar|ablation|baseline|related|reference|acknowledg|example|supplement", re.I
)


def _hidden_or_footer(ancestors):
    return any(
        parent.name == "footer"
        or parent.has_attr("hidden")
        or str(parent.get("aria-hidden", "")).lower() == "true"
        or re.search(r"display\s*:\s*none|visibility\s*:\s*hidden", parent.get("style", ""), re.I)
        for parent in ancestors
    )


def _labels(ancestors):
    labels = " ".join(
        str(parent.get(key, ""))
        for parent in ancestors[:4]
        for key in ("id", "class", "title", "aria-label")
    )
    return re.sub(r"([a-z])([A-Z])", r"\1 \2", labels)


def _section_headings(soup):
    """Map media nodes to their nearest preceding h2-h4 text in one linear pass."""
    headings = {}
    current = None
    for node in soup.find_all(["h2", "h3", "h4", "video", "iframe", "a", "img"]):
        if node.name in {"h2", "h3", "h4"}:
            current = re.sub(r"^\s*\d+[\s.\-:]*", "", node.get_text(" ", strip=True))
        else:
            headings[id(node)] = current
    return headings


def _video_score(node, reason, heading=None):
    """Score semantic placement, without pretending DOM order measures the viewport.

    ``heading`` is the nearest preceding section heading, or None before any heading.
    """
    if node is None:
        return (60, ["page preview video"]) if reason == "page preview video" else (0, [])
    ancestors = [node, *node.parents]
    if _hidden_or_footer(ancestors):
        return -100, ["hidden or footer media"]
    context = heading or ""
    labels = _labels(ancestors)
    if _SECONDARY_SECTION.search(context + " " + labels):
        return -80, ["experimental or supplementary section"]
    early = heading is None or bool(re.fullmatch(r"abstract|introduction", context, re.I))
    text = node.get_text(" ", strip=True) if node.name == "a" else ""
    if early and re.fullmatch(
        r"(?:watch\s+)?(?:video|demo|overview|teaser)(?:\s+video)?", text, re.I
    ):
        return 100, ["explicit video action near the title"]
    if re.search(
        r"\b(teaser|overview|trailer|main.video|demo)\b", labels + " " + context, re.I
    ) or re.fullmatch(r"(?:video|demo)(?:\s+video)?", context, re.I):
        return 80, ["explicit teaser or overview label"]
    if early and re.search(r"\b(hero|demo)\b", labels, re.I):
        return 70, ["early hero or demo player"]
    if (
        early
        and node.name in {"video", "iframe"}
        and (_integer(node.get("width")) or 0) >= 640
        and (_integer(node.get("height")) or 0) >= 300
    ):
        return 60, ["large player before detailed sections"]
    return 0, ["no strong main-video signal"]


def select_main_video(candidates, *, allow_player_fallback=False):
    """Select at most one confident video; old unscored candidates stay unselected."""
    candidates = list(candidates)
    eligible = [c for c in candidates if c.media_type == "video" and c.main_video_score >= 60]
    if not eligible and allow_player_fallback:
        # Preserve explicit exclusions; never promote network-only observations.
        eligible = [
            c
            for c in candidates
            if c.media_type == "video"
            and c.main_video_score == 0
            and any(e.startswith(("video element", "embedded video")) for e in c.evidence)
        ]
    return max(eligible, key=lambda c: c.main_video_score, default=None)


def _image_adjustment(node, heading):
    """Prefer early, large figures; demote secondary sections instead of trusting DOM order."""
    ancestors = [node, *node.parents]
    if _hidden_or_footer(ancestors):
        return -100, "hidden or footer image"
    if _SECONDARY_SECTION.search((heading or "") + " " + _labels(ancestors)):
        return -30, "secondary section image"
    width = _integer(node.get("width")) or 0
    adjustment, reason = 0, ""
    if heading is None or re.fullmatch(r"abstract|introduction|overview|method", heading, re.I):
        adjustment, reason = 10, "early image"
    if width >= 600:
        adjustment += 10
        reason = (reason + "; " if reason else "") + "large image"
    return adjustment, reason


def discover_visuals(
    html: str,
    page_url: str,
    *,
    observed_media: Iterable[str] = (),
    soup: BeautifulSoup | None = None,
) -> list[VisualCandidate]:
    """Rank visual candidates; pass an existing ``soup`` of ``html`` to avoid reparsing."""
    soup = soup if soup is not None else BeautifulSoup(html, "html.parser")
    base_node = soup.find("base", href=True)
    base = (absolute_url(base_node["href"], page_url) if base_node else page_url) or page_url
    headings = _section_headings(soup)
    candidates = {}
    scores = {}

    def add(
        raw,
        kind,
        *,
        delivery="direct",
        node=None,
        reason="page media",
        score=0,
        poster=None,
        group=None,
    ):
        url = absolute_url(raw or "", base)
        if not url:
            return
        provider, provider_id = video_identity(url)
        if provider_id:
            kind, delivery = "video", "embed"
        width = _integer(node.get("width")) if node is not None else None
        height = _integer(node.get("height")) if node is not None else None
        label = (
            " ".join(
                [
                    str(node.get("alt", "")),
                    str(node.get("class", "")),
                    str(node.get("id", "")),
                    str(node.get("title", "")),
                ]
            )
            if node is not None
            else ""
        )
        if kind == "image":
            if re.search(
                r"\b(logo|icon|avatar|badge|favicon)\b", label + " " + urlsplit(url).path, re.I
            ):
                return
            if (width is not None and width < 160) or (height is not None and height < 80):
                return
        if node is not None and (
            node.has_attr("hidden") or re.search(r"display\s*:\s*none", node.get("style", ""))
        ):
            return
        if re.search(r"hero|teaser|overview|result|demo", label, re.I):
            score += 40
            reason += "; main-visual label"
        heading = headings.get(id(node)) if node is not None else None
        if kind == "image" and node is not None:
            adjustment, note = _image_adjustment(node, heading)
            score += adjustment
            if note:
                reason += "; " + note
        # Sources of one <video> element share its group, so they merge as alternatives.
        identity = f"{provider}:{provider_id}" if provider_id else group or url
        main_score, main_reasons = (
            _video_score(node, reason, heading) if kind == "video" else (0, [])
        )
        source = VisualSource(
            url=url, delivery=delivery, provider=provider, provider_id=provider_id
        )
        if identity in candidates:
            previous = candidates[identity]
            if reason == "observed video response":
                main_score = previous.main_video_score
                main_reasons = previous.main_video_reasons
            alternatives = list(previous.alternatives)
            if source.url != previous.url and source not in alternatives:
                alternatives.append(source)
            candidates[identity] = previous.model_copy(
                update={
                    "alternatives": alternatives,
                    "evidence": list(dict.fromkeys(previous.evidence + [reason])),
                    "poster_url": previous.poster_url or absolute_url(poster or "", base),
                    "main_video_score": max(previous.main_video_score, main_score),
                    "main_video_reasons": (
                        main_reasons
                        if main_score > previous.main_video_score
                        else previous.main_video_reasons
                    ),
                }
            )
            scores[identity] = max(scores[identity], score)
            return
        candidates[identity] = VisualCandidate(
            id=hashlib.sha256(identity.encode()).hexdigest()[:20],
            url=url,
            media_type=kind,
            delivery=delivery,
            source_page_url=page_url,
            evidence=[reason],
            poster_url=absolute_url(poster or "", base),
            provider=provider,
            provider_id=provider_id,
            width=width,
            height=height,
            selection_reason=reason,
            main_video_score=main_score,
            main_video_reasons=main_reasons,
        )
        scores[identity] = score

    for node in soup.find_all("video"):
        sources = [node.get("src", "")] + [item.get("src", "") for item in node.find_all("source")]
        resolved = [url for url in (absolute_url(source or "", base) for source in sources) if url]
        group = resolved[0] if resolved else None
        if group and video_identity(group)[1]:
            group = None
        for source in resolved:
            add(
                source,
                "video",
                node=node,
                score=50,
                reason="video element",
                poster=node.get("poster"),
                group=group,
            )
    for node in soup.find_all(["iframe", "a"]):
        raw = node.get("src") or node.get("href") or ""
        target = absolute_url(raw, base)
        if target and video_identity(target)[1]:
            add(target, "video", delivery="embed", node=node, score=45, reason="embedded video")
        elif (
            target
            and node.name == "a"
            and urlsplit(target).path.lower().endswith((".mp4", ".webm", ".ogv"))
        ):
            add(target, "video", node=node, score=45, reason="video link")
        elif (
            target
            and node.name == "iframe"
            and re.search(
                r"hero|teaser|demo|overview",
                " ".join([str(node.get("class", "")), node.get("title", "")]),
                re.I,
            )
        ):
            add(
                target,
                "unknown",
                delivery="embed",
                node=node,
                score=15,
                reason="embedded demonstration",
            )
    for node in soup.find_all("img"):
        source = node.get("src") or ""
        if not absolute_url(source, base):
            source = node.get("data-src") or ""
        srcset = node.get("srcset") or node.get("data-srcset")
        if not srcset and node.parent.name == "picture":
            picture_source = node.parent.find("source", srcset=True)
            srcset = picture_source.get("srcset") if picture_source else None
        if srcset:
            entries = [part.strip().split() for part in srcset.split(",") if part.strip()]
            if entries:
                source = entries[-1][0]
        add(source, "image", node=node, score=20, reason="image element")
    for node in soup.find_all("meta"):
        name = (node.get("property") or node.get("name") or "").lower()
        if name in {"og:image", "twitter:image", "twitter:image:src"}:
            add(node.get("content"), "image", score=35, reason="page preview image")
        elif name in {"og:video", "og:video:url", "og:video:secure_url"}:
            add(node.get("content"), "video", score=40, reason="page preview video")
    for url in observed_media:
        add(url, "video", score=10, reason="observed video response")
    posters = {candidate.poster_url for candidate in candidates.values() if candidate.poster_url}
    ordered = sorted(candidates, key=lambda key: -scores[key])
    result = []
    for key in ordered:
        candidate = candidates[key]
        if candidate.media_type == "image" and candidate.url in posters:
            continue
        result.append(candidate.model_copy(update={"rank": len(result) + 1}))
    return result
