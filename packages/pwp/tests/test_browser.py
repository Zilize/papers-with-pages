"""Real Chromium tests with intercepted fixtures; no external network is required."""

import asyncio
import os

import pytest
from helpers import FakeFetcher
from pwp import render_page

pytestmark = pytest.mark.skipif(
    os.environ.get("PWP_BROWSER_TESTS") != "1",
    reason="Set PWP_BROWSER_TESTS=1 after installing Chromium",
)


async def test_hero_and_dynamic_dom_share_one_visit(tmp_path):
    html = """<!doctype html><html><head><title>Rendered research</title></head><body>
    <h1>Rendered research</h1><script>
      const a = document.createElement('a');
      a.href = 'https://github.com/example/project'; a.textContent = 'Code';
      document.body.appendChild(a);
      fetch('http://169.254.169.254/latest/meta-data/').catch(() => {});
    </script></body></html>"""
    fetcher = FakeFetcher({"https://project.example/": html})
    result = await render_page(
        "https://project.example/",
        work_dir=tmp_path,
        fetcher=fetcher,
        viewport=(800, 600),
        settle_ms=100,
    )
    assert result.page.title == "Rendered research"
    assert result.page.links[0].kind == "repository"
    assert result.screenshot.kind == "hero_screenshot"
    assert (result.screenshot.width, result.screenshot.height) == (800, 600)
    assert sum(url == "https://project.example/" for url, _ in fetcher.calls) == 1
    assert not any("169.254" in url for url, _ in fetcher.calls)
    assert any(issue.code == "blocked_url" for issue in result.issues)


async def test_analytics_requests_are_skipped_without_asset_warnings(tmp_path):
    html = """<h1>Project</h1><script>
    fetch('https://www.google-analytics.com/g/collect', {method: 'POST'}).catch(() => {});
    fetch('https://www.youtube.com/youtubei/v1/log_event', {method: 'POST'}).catch(() => {});
    </script>"""
    fetcher = FakeFetcher({"https://project.example/": html})
    result = await render_page("https://project.example/", work_dir=tmp_path, fetcher=fetcher)
    assert len(fetcher.calls) == 1
    assert not result.issues
    assert result.screenshot.path.exists()


async def test_navigation_without_committed_document_is_a_recoverable_render_failure(monkeypatch):
    from playwright.async_api import Page, TimeoutError
    from pwp.errors import PwpError

    async def timeout(*args, **kwargs):
        raise TimeoutError("Navigation did not commit")

    monkeypatch.setattr(Page, "goto", timeout)
    with pytest.raises(PwpError) as caught:
        await render_page(
            "https://project.example/",
            fetcher=FakeFetcher(
                {
                    "https://project.example/": "<h1>Project</h1>",
                }
            ),
        )
    assert caught.value.issue.code == "render_failed"
    assert caught.value.issue.retryable


async def test_slow_asset_does_not_prevent_hero(tmp_path):
    class SlowFetcher(FakeFetcher):
        async def get(self, url, **kwargs):
            if url.endswith("slow.js"):
                await asyncio.Event().wait()
            return await super().get(url, **kwargs)

    fetcher = SlowFetcher(
        {
            "https://project.example/": (
                '<html><head><title>Slow asset</title><script src="/slow.js"></script>'
                "</head><body><h1>Research</h1></body></html>"
            )
        }
    )
    result = await render_page(
        "https://project.example/", work_dir=tmp_path, fetcher=fetcher, timeout_ms=1000, settle_ms=0
    )
    assert result.screenshot.path.exists()
    assert any(issue.code == "asset_timeout" for issue in result.issues)


async def test_session_reuses_browser_isolates_storage_and_recovers():
    from pwp import RenderSession

    async with RenderSession(concurrency=2) as session:
        assert session._browser is None
        async with session.context(viewport=(400, 300), scale=1) as first:
            browser = session._browser
            await first.add_cookies(
                [{"name": "secret", "value": "first", "url": "https://example.org"}]
            )
            async with session.context(viewport=(400, 300), scale=1) as second:
                assert session._browser is browser
                assert await second.cookies() == []
                entered = asyncio.Event()

                async def third():
                    async with session.context(viewport=(400, 300), scale=1):
                        entered.set()

                queued = asyncio.create_task(third())
                await asyncio.sleep(0)
                assert not entered.is_set()
            await asyncio.wait_for(queued, 5)
        assert browser.contexts == []
        await browser.close()
        async with session.context(viewport=(400, 300), scale=1):
            assert session._browser is not browser
            assert session._browser.is_connected()
    assert session._browser is None
    assert session._proxy is None


async def test_shared_browser_preserves_per_paper_request_budgets():
    from pwp import RenderSession
    from pwp.network import RequestBudget, request_scope

    html = '<html><head><title>Budget</title><script src="/asset.js"></script></head></html>'
    fetcher = FakeFetcher(
        {"https://project.example/": html, "https://project.example/asset.js": ""}
    )
    budgets = [RequestBudget(10), RequestBudget(10)]
    async with RenderSession() as session:
        for budget in budgets:
            with request_scope(budget):
                await render_page(
                    "https://project.example/", fetcher=fetcher, render_session=session
                )
        assert [budget.used for budget in budgets] == [2, 2]


async def test_cancelled_context_releases_slot():
    from pwp import RenderSession

    async with RenderSession(concurrency=1) as session:
        entered = asyncio.Event()

        async def waiting():
            async with session.context(viewport=(400, 300), scale=1):
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(waiting())
        await asyncio.wait_for(entered.wait(), 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        async with asyncio.timeout(5):
            async with session.context(viewport=(400, 300), scale=1):
                assert len(session._browser.contexts) == 1


@pytest.mark.parametrize("operation", ["fulfill", "abort"])
async def test_route_completion_error_does_not_poison_shared_browser(
    monkeypatch, capsys, operation
):
    from playwright.async_api import Error, Route
    from pwp import RenderSession

    original = getattr(Route, operation)
    completions = []

    async def fail_after_completion(route, *args, **kwargs):
        await original(route, *args, **kwargs)
        if route.request.url.endswith("/asset.js"):
            completions.append(route.request.url)
            raise Error("Route is already handled!")

    monkeypatch.setattr(Route, operation, fail_after_completion)
    fixtures = {
        "https://project.example/": '<script src="/asset.js"></script><h1>Research</h1>',
        "https://project.example/next": "<title>Next paper</title>",
    }
    if operation == "fulfill":
        fixtures["https://project.example/asset.js"] = ""
    fetcher = FakeFetcher(fixtures)
    async with RenderSession(concurrency=1) as session:
        first = await render_page(
            "https://project.example/", fetcher=fetcher, render_session=session, settle_ms=0
        )
        second = await render_page(
            "https://project.example/next", fetcher=fetcher, render_session=session, settle_ms=0
        )
        assert second.page.title == "Next paper"
        assert any(issue.code == "asset_route_failed" for issue in first.issues)
        assert completions == ["https://project.example/asset.js"]
        assert session._browser.contexts == []
    assert "Error occurred in event listener" not in capsys.readouterr().err


async def test_finished_capture_cancels_only_its_own_asset_fetches():
    from pwp import RenderSession

    active = set()
    started = asyncio.Event()

    class HangingFetcher(FakeFetcher):
        async def get(self, url, **kwargs):
            if url.endswith("slow.png"):
                active.add(url)
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    active.discard(url)
            return await super().get(url, **kwargs)

    fetcher = HangingFetcher(
        {
            "https://project.example/": '<h1>Ready</h1><img src="/slow.png">',
            "https://project.example/next": "<title>Next paper</title>",
        }
    )
    async with asyncio.timeout(10), RenderSession(concurrency=2) as session:
        capture = asyncio.create_task(
            render_page(
                "https://project.example/",
                fetcher=fetcher,
                render_session=session,
                settle_ms=0,
            )
        )
        await started.wait()
        await capture
        assert not active
        result = await render_page(
            "https://project.example/next", fetcher=fetcher, render_session=session, settle_ms=0
        )
        assert result.page.title == "Next paper"


async def test_hero_only_fetches_visible_video_ranges_and_reports_bad_media(tmp_path):
    from pwp.network import FetchResult

    class MediaFetcher(FakeFetcher):
        async def get(self, url, **kwargs):
            if url.endswith(".mp4"):
                self.calls.append((url, kwargs))
                return FetchResult(url, url, 206, {"content-type": "video/mp4"}, b"invalid")
            return await super().get(url, **kwargs)

    fetcher = MediaFetcher(
        {
            "https://project.example/": """<html><body>
        <video width="400" height="300" autoplay muted src="/visible.mp4"></video>
        <video style="display:none" src="/hidden.mp4"></video>
        <div style="height:2000px"></div><video src="/offscreen.mp4"></video>
        </body></html>"""
        }
    )
    result = await render_page("https://project.example/", fetcher=fetcher, work_dir=tmp_path)
    media = [(url, options) for url, options in fetcher.calls if url.endswith(".mp4")]
    assert media and all(url.endswith("/visible.mp4") for url, _ in media)
    assert sum(options["max_bytes"] for _, options in media) <= 8 * 1024 * 1024
    assert all(options["headers"]["Range"].startswith("bytes=") for _, options in media)
    assert any(issue.code == "video_frame_unavailable" for issue in result.issues)
    assert result.screenshot.path.exists()


async def test_hero_contains_decoded_video_pixels(tmp_path):
    from PIL import Image
    from playwright.async_api import async_playwright
    from pwp.network import FetchResult

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page()
        data = await page.evaluate("""async () => {
          const c = document.createElement('canvas'); c.width = 320; c.height = 240;
          const ctx = c.getContext('2d'); ctx.fillStyle = '#ff0000';
          ctx.fillRect(0, 0, 320, 240);
          const stream = c.captureStream(10), chunks = [];
          const recorder = new MediaRecorder(stream, {mimeType:'video/webm;codecs=vp8'});
          recorder.ondataavailable = e => chunks.push(e.data);
          const stopped = new Promise(r => recorder.onstop = r);
          recorder.start(); await new Promise(r => setTimeout(r, 300));
          recorder.stop(); await stopped; stream.getTracks().forEach(t => t.stop());
          return [...new Uint8Array(await new Blob(chunks).arrayBuffer())];
        }""")
        await browser.close()

    class VideoFetcher(FakeFetcher):
        async def get(self, url, **kwargs):
            if url.endswith(".webm"):
                await asyncio.sleep(0.4)
                return FetchResult(url, url, 200, {"content-type": "video/webm"}, bytes(data))
            return await super().get(url, **kwargs)

    fetcher = VideoFetcher(
        {
            "https://project.example/": (
                '<video width="320" height="240" autoplay muted src="/red.webm"></video>'
            )
        }
    )
    result = await render_page("https://project.example/", fetcher=fetcher, work_dir=tmp_path)
    assert not any(i.code == "video_frame_unavailable" for i in result.issues)
    with Image.open(result.screenshot.path) as screenshot:
        red, green, blue = screenshot.convert("RGB").getpixel((100, 100))
        assert red > 200 and green < 50 and blue < 50


async def test_suspended_animation_frames_do_not_stall_capture(tmp_path):
    fetcher = FakeFetcher(
        {
            "https://project.example/": (
                "<script>window.requestAnimationFrame = () => 0;</script><h1>Paused frames</h1>"
            )
        }
    )
    async with asyncio.timeout(10):
        result = await render_page(
            "https://project.example/", fetcher=fetcher, work_dir=tmp_path, settle_ms=0
        )
    assert result.screenshot.path.exists()


async def test_delayed_manifest_initializes_hero_before_capture(tmp_path):
    from PIL import Image

    class ManifestFetcher(FakeFetcher):
        async def get(self, url, **kwargs):
            if url.endswith("clips.json"):
                await asyncio.sleep(1)
            return await super().get(url, **kwargs)

    fetcher = ManifestFetcher(
        {
            "https://project.example/": """<div id="hero"></div><script>
          fetch('/clips.json').then(r => r.json()).then(() => {
            document.getElementById('hero').innerHTML =
              '<div style="position:fixed;inset:0;background:rgb(255,0,0)">Loaded demo</div>';
          });</script>""",
            "https://project.example/clips.json": {"clips": []},
        }
    )
    result = await render_page(
        "https://project.example/", work_dir=tmp_path, fetcher=fetcher, settle_ms=0
    )
    assert "Loaded demo" in result.html
    with Image.open(result.screenshot.path) as image:
        assert image.convert("RGB").getpixel((100, 100)) == (255, 0, 0)


async def test_hung_route_cleanup_releases_browser_slot(monkeypatch):
    from playwright.async_api import BrowserContext
    from pwp import RenderSession

    monkeypatch.setattr("pwp.browser.CONTEXT_CLEANUP_TIMEOUT", 0.1)
    original = BrowserContext.unroute_all
    calls = []

    async def stalled(context, *, behavior=None):
        calls.append(behavior)
        if behavior == "wait":
            await asyncio.Event().wait()
        return await original(context, behavior=behavior)

    monkeypatch.setattr(BrowserContext, "unroute_all", stalled)
    async with asyncio.timeout(10), RenderSession(concurrency=1) as session:
        for _ in range(2):
            async with session.context(viewport=(400, 300), scale=1):
                pass
        assert session._browser.contexts == []
        monkeypatch.setattr("pwp.browser.CONTEXT_CLEANUP_TIMEOUT", 5)
    assert calls == ["ignoreErrors", "ignoreErrors"]


async def test_late_asset_completion_does_not_cancel_driver_notifications(monkeypatch, capsys):
    from pwp import RenderSession

    monkeypatch.setattr("pwp.browser.CONTEXT_CLEANUP_TIMEOUT", 0.2)

    class LateFetcher(FakeFetcher):
        async def get(self, url, **kwargs):
            if url.endswith("late.png"):
                await asyncio.sleep(1)
            return await super().get(url, **kwargs)

    fetcher = LateFetcher(
        {
            "https://project.example/": '<h1>First</h1><img src="/late.png">',
            "https://project.example/late.png": "image",
            "https://project.example/next": "<title>Next paper</title>",
        }
    )
    async with RenderSession(concurrency=1) as session:
        await render_page(
            "https://project.example/", fetcher=fetcher, render_session=session, settle_ms=0
        )
        await asyncio.sleep(1)
        result = await render_page(
            "https://project.example/next", fetcher=fetcher, render_session=session, settle_ms=0
        )
        assert result.page.title == "Next paper"
        monkeypatch.setattr("pwp.browser.CONTEXT_CLEANUP_TIMEOUT", 5)
    assert "Error occurred in event listener" not in capsys.readouterr().err


async def test_concurrent_captures_leave_no_pending_downloads_or_driver_errors(capsys):
    from pwp import RenderSession

    pending = set()

    class SlowImages(FakeFetcher):
        async def get(self, url, **kwargs):
            if url.endswith(".png"):
                task = asyncio.current_task()
                pending.add(task)
                try:
                    await asyncio.sleep(30)
                finally:
                    pending.discard(task)
            return await super().get(url, **kwargs)

    urls = [f"https://project.example/{i}" for i in range(6)]
    fetcher = SlowImages({url: '<h1>Research</h1><img src="/slow.png">' for url in urls})
    async with asyncio.timeout(20), RenderSession(concurrency=6) as session:
        for _ in range(3):
            results = await asyncio.gather(
                *(
                    render_page(
                        url,
                        fetcher=fetcher,
                        render_session=session,
                        settle_ms=0,
                    )
                    for url in urls
                )
            )
            assert len(results) == 6
            assert not pending
            assert not session._browser.contexts
    assert "Error occurred in event listener" not in capsys.readouterr().err
