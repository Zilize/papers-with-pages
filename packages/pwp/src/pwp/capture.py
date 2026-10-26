"""Render with intercepted, validated HTTP and a rejecting fallback proxy."""

import asyncio
import re
from contextvars import copy_context
from dataclasses import dataclass, field

from pwp.browser import RenderSession
from pwp.errors import PwpError, RequestError
from pwp.events import EventReporter
from pwp.extract.links import is_telemetry_url, normalize_url
from pwp.media import save_image
from pwp.models import Issue, MediaArtifact, ProjectPage
from pwp.network import RequestBudget, SafeFetcher, require_success, validate_target
from pwp.page import parse_project_page

HERO_READY_TIMEOUT = 7
PAGE_TIMEOUT = 60
BOOTSTRAP_TIMEOUT = 8
# Subresources have their own allowance so heavy pages cannot starve link discovery.
MAX_PAGE_ASSETS = 300


class _CaptureClosed(Exception):
    """An asset fetch was cancelled because its owning capture finished."""


class _RouteRequests:
    """Cancel HTTP work on exit without cancelling Playwright route handlers."""

    def __init__(self):
        self.tasks = set()
        self.closed = False

    async def __aenter__(self):
        return self

    async def get(self, fetcher, *args, **kwargs):
        if self.closed:
            raise _CaptureClosed
        task = asyncio.create_task(fetcher.get(*args, **kwargs))
        self.tasks.add(task)
        try:
            return await task
        except asyncio.CancelledError:
            if self.closed:
                raise _CaptureClosed from None
            raise
        finally:
            self.tasks.discard(task)

    async def __aexit__(self, *args):
        self.closed = True
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _wait_for_dynamic_content(pending):
    """Allow async page initialization to settle without waiting for media streams."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + BOOTSTRAP_TIMEOUT
    quiet_since = loop.time()
    while loop.time() < deadline:
        if pending:
            quiet_since = loop.time()
        elif loop.time() - quiet_since >= 0.5:
            return True
        await asyncio.sleep(0.1)
    return False


async def _prepare_hero(page, allowed_media):
    """Load only visible native players and bound visual readiness to five seconds."""
    urls = await page.evaluate("""() => {
      const visible = el => {
        const r = el.getBoundingClientRect(), s = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && r.top < innerHeight && r.bottom > 0
          && r.left < innerWidth && r.right > 0 && s.visibility !== 'hidden'
          && s.display !== 'none' && s.opacity !== '0';
      };
      window.__pwpVideos = [...document.querySelectorAll('video')].filter(visible).slice(0, 2);
      window.__pwpImages = [...document.images].filter(visible);
      return window.__pwpVideos.flatMap(v => [v.currentSrc, v.src,
        ...[...v.querySelectorAll('source')].map(s => s.src)]).filter(Boolean);
    }""")
    allowed_media.update(urls)
    return await page.evaluate("""async () => {
      const videos = window.__pwpVideos, images = window.__pwpImages;
      const ready = (el, event, test) => test() ? Promise.resolve() :
        new Promise(resolve => {
          el.addEventListener(event, resolve, {once:true});
          el.addEventListener('error', resolve, {once:true});
        });
      const waits = videos.map(v => {
        v.pause(); v.muted = true; v.autoplay = false; v.preload = 'auto';
        if (v.readyState < 2) v.load();
        return ready(v, 'loadeddata', () => v.readyState >= 2);
      });
      waits.push(...images.map(i => ready(i, 'load', () => i.complete)));
      waits.push(document.fonts.ready);
      let timer;
      await Promise.race([Promise.all(waits), new Promise(r => timer = setTimeout(r, 5000))]);
      clearTimeout(timer);
      videos.forEach(v => v.pause());
      await Promise.race([
        new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))),
        new Promise(r => setTimeout(r, 250))
      ]);
      return {videos: videos.filter(v => v.readyState < 2).length,
        images: images.filter(i => !i.complete || !i.naturalWidth).length,
        fonts: document.fonts.status !== 'loaded'};
    }""")


@dataclass
class RenderResult:
    page: ProjectPage
    html: str
    screenshot: MediaArtifact | None = None
    issues: list[Issue] = field(default_factory=list)


async def capture_hero(page, *, work_dir, source_url=None, viewport=(1440, 900), scale=1):
    """Capture a caller-owned Playwright page's first viewport, never a full page."""
    if min(viewport) < 1 or max(viewport) > 4096 or not 0 < scale <= 2:
        raise ValueError("Invalid viewport or device scale")
    await page.set_viewport_size({"width": viewport[0], "height": viewport[1]})
    data = await page.screenshot(type="png", full_page=False, animations="disabled", timeout=15000)
    return save_image(
        data,
        work_dir,
        kind="hero_screenshot",
        source_url=source_url or page.url,
        viewport=viewport,
        device_scale_factor=scale,
    )


async def render_page(
    url,
    *,
    work_dir=None,
    fetcher=None,
    render_session=None,
    viewport=(1440, 900),
    scale=1,
    timeout_ms=20000,
    settle_ms=300,
    on_event=None,
) -> RenderResult:
    """Render a project page and optionally capture its hero in the same browser visit."""
    if min(viewport) < 1 or max(viewport) > 4096 or not 0 < scale <= 2:
        raise ValueError("Invalid viewport or device scale")
    if timeout_ms <= 0 or not 0 <= settle_ms <= 5000:
        raise ValueError("Invalid render timing")
    try:
        from playwright.async_api import Error as BrowserError
    except ImportError as exc:
        raise PwpError(
            "render_unavailable", "Install the render extra and Playwright Chromium"
        ) from exc
    if fetcher is None:
        async with SafeFetcher() as owned:
            return await render_page(
                url,
                work_dir=work_dir,
                fetcher=owned,
                render_session=render_session,
                viewport=viewport,
                scale=scale,
                timeout_ms=timeout_ms,
                settle_ms=settle_ms,
                on_event=on_event,
            )
    if render_session is None:
        async with RenderSession() as owned_session:
            return await render_page(
                url,
                work_dir=work_dir,
                fetcher=fetcher,
                render_session=owned_session,
                viewport=viewport,
                scale=scale,
                timeout_ms=timeout_ms,
                settle_ms=settle_ms,
                on_event=on_event,
            )
    reporter = EventReporter("render_page", on_event)
    reporter.emit("step_started", resource_url=url)
    initial = await fetcher.get(url)
    require_success(initial)
    if "html" not in initial.headers.get("content-type", "").lower():
        raise RequestError("not_html", "The resource is not an HTML project page")
    issues = []
    observed = []
    allowed_media = set()
    media_remaining = 8 * 1024 * 1024
    preloaded = {initial.final_url: initial}
    # Browser subresources skip robots checks and crawl pacing, like a reader's browser.
    asset_budget = RequestBudget(MAX_PAGE_ASSETS)
    request_context = copy_context()
    pending_bootstrap = set()
    reporter.emit("step_progress", unit="browser_slot")
    try:
        async with (
            render_session.context(viewport=viewport, scale=scale) as context,
            _RouteRequests() as requests,
            asyncio.timeout(PAGE_TIMEOUT),
        ):

            async def handle_route(route):
                nonlocal media_remaining
                request = route.request
                try:
                    if is_telemetry_url(request.url):
                        await route.abort("blockedbyclient")
                        return
                    if request.method not in {"GET", "HEAD"}:
                        raise RequestError(
                            "method_blocked", "Only read-only browser requests are allowed"
                        )
                    if request.resource_type == "media":
                        observed.append(validate_target(request.url))
                        if request.url not in allowed_media or media_remaining <= 0:
                            await route.abort("blockedbyclient")
                            return
                        # Reserve before awaiting so concurrent ranges share a strict budget.
                        size = min(media_remaining, 2 * 1024 * 1024)
                        media_remaining -= size
                        match = re.fullmatch(
                            r"bytes=(\d+)-(\d*)", request.headers.get("range", "bytes=0-")
                        )
                        if not match:
                            await route.abort("blockedbyclient")
                            return
                        start = int(match[1])
                        end = min(start + size - 1, int(match[2])) if match[2] else start + size - 1
                        async with asyncio.timeout(5):
                            response = await requests.get(
                                fetcher,
                                request.url,
                                headers={"Range": f"bytes={start}-{end}"},
                                max_bytes=size,
                                asset_budget=asset_budget,
                            )
                        require_success(response)
                    else:
                        response = preloaded.pop(request.url, None)
                    if response is None:
                        asset_timeout = (
                            20
                            if request.resource_type
                            in {"document", "image", "stylesheet", "font", "script"}
                            else 8
                        )
                        async with asyncio.timeout(min(timeout_ms / 1000, asset_timeout)):
                            response = await requests.get(
                                fetcher, request.url, omit_video=True, asset_budget=asset_budget
                            )
                    content_type = response.headers.get("content-type", "")
                    if content_type.startswith("video/") and request.resource_type != "media":
                        observed.append(response.final_url)
                        await route.abort("blockedbyclient")
                        return
                    # Compression and validated redirects were handled by the fetcher.
                    headers = {
                        key: value
                        for key, value in response.headers.items()
                        if key
                        not in {
                            "content-encoding",
                            "content-length",
                            "transfer-encoding",
                            "connection",
                            "set-cookie",
                            "location",
                            "refresh",
                            "alt-svc",
                        }
                    }
                    await route.fulfill(status=response.status, headers=headers, body=response.body)
                except _CaptureClosed:
                    await route.abort("aborted")
                except PwpError as exc:
                    if len(issues) < 30:
                        issues.append(exc.issue.model_copy(update={"resource_url": request.url}))
                    await route.abort("blockedbyclient")
                except TimeoutError:
                    if len(issues) < 30:
                        issues.append(
                            Issue(
                                code="asset_timeout",
                                message="A page asset timed out",
                                retryable=True,
                                resource_url=request.url,
                            )
                        )
                    await route.abort("timedout")

            async def route_request(route):
                # Playwright dispatches on its driver context, not the current paper task.
                if route.request.resource_type in {"script", "stylesheet", "xhr", "fetch"}:
                    pending_bootstrap.add(route)
                try:
                    await asyncio.create_task(handle_route(route), context=request_context.copy())
                except BrowserError as exc:
                    # A cancelled/finished browser request can reject fulfill or abort.
                    # Do not handle it again or leak the error into the shared driver.
                    if len(issues) < 30:
                        issues.append(
                            Issue(
                                code="asset_route_failed",
                                message=str(exc)[:500],
                                resource_url=route.request.url,
                            )
                        )
                finally:
                    pending_bootstrap.discard(route)

            await context.route("**/*", route_request)
            await context.route_web_socket("**/*", lambda ws: ws.close())
            page = await context.new_page()
            try:
                reporter.emit("step_progress", unit="navigation")
                await page.goto(
                    initial.final_url, wait_until="domcontentloaded", timeout=timeout_ms
                )
                await page.wait_for_timeout(settle_ms)
                reporter.emit("step_progress", unit="dynamic_content")
                if not await _wait_for_dynamic_content(pending_bootstrap):
                    issues.append(
                        Issue(
                            code="dynamic_content_incomplete",
                            retryable=True,
                            message="Page initialization exceeded the capture deadline",
                        )
                    )
                if work_dir is not None:
                    reporter.emit("step_progress", unit="visual_readiness")
                    async with asyncio.timeout(HERO_READY_TIMEOUT):
                        readiness = await _prepare_hero(page, allowed_media)
                    allowed_media.clear()
                    if readiness["videos"]:
                        issues.append(
                            Issue(
                                code="video_frame_unavailable",
                                message="A visible video frame was not ready in time",
                            )
                        )
                    if readiness["images"] or readiness["fonts"]:
                        issues.append(
                            Issue(
                                code="visual_assets_incomplete",
                                message="Some first-viewport images or fonts were not ready",
                            )
                        )
            except (BrowserError, TimeoutError) as exc:
                issues.append(
                    Issue(
                        code="render_incomplete",
                        retryable=True,
                        message=str(exc)[:500] or "Visual readiness exceeded its time budget",
                    )
                )
            allowed_media.clear()
            try:
                normalize_url(page.url)
            except ValueError as exc:
                raise PwpError(
                    "render_failed", "Browser navigation did not reach an HTTP page", retryable=True
                ) from exc
            reporter.emit("step_progress", unit="read_dom")
            async with asyncio.timeout(timeout_ms / 1000):
                html = await page.content()
            # Parsing is CPU-bound; keep the shared event loop responsive.
            parsed = await asyncio.to_thread(
                parse_project_page,
                html,
                page.url,
                original_url=url,
                http_status=initial.status,
                observed_media=list(observed),
            )
            screenshot = None
            if work_dir is not None:
                try:
                    reporter.emit("step_progress", unit="screenshot")
                    screenshot = await capture_hero(
                        page,
                        work_dir=work_dir,
                        source_url=page.url,
                        viewport=viewport,
                        scale=scale,
                    )
                except BrowserError as exc:
                    issues.append(Issue(code="capture_failed", message=str(exc)[:500]))
            reporter.emit("step_progress", unit="cleanup")
    except (BrowserError, TimeoutError) as exc:
        raise PwpError(
            "render_failed",
            str(exc)[:500] or "Page rendering exceeded its time budget",
            retryable=True,
        ) from exc
    reporter.emit("step_completed", resource_url=url)
    return RenderResult(parsed, html, screenshot, issues)
