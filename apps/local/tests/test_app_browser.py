"""Offline browser coverage for collection, gallery navigation, and mobile layout."""

import asyncio
import copy
import os
import socket
import threading
from datetime import date

import pytest
import uvicorn
from app.server import create_app
from app.storage import Store
from pwp.models import (
    Author,
    HarvestBatch,
    HarvestRecord,
    Paper,
    PaperResult,
    ResourceLink,
    VisualCandidate,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("PWP_BROWSER_TESTS") != "1", reason="Set PWP_BROWSER_TESTS=1 for Chromium"
)


async def test_gallery_collection_and_mobile(tmp_path):
    from playwright.async_api import async_playwright

    papers = [
        Paper(
            arxiv_id="2601.10001",
            title="Learning to see a world in motion",
            abstract="Synthetic browser fixture for a visual research project.",
            authors=[Author(name="Alex Researcher"), Author(name="Sam Scientist")],
            categories=["cs.CV", "cs.AI"],
            primary_category="cs.CV",
            published_at=date(2026, 1, 3),
        ),
        Paper(
            arxiv_id="2601.10002",
            title="Small models, broader possibilities",
            authors=[Author(name="Taylor Researcher")],
            categories=["cs.LG"],
            primary_category="cs.LG",
            published_at=date(2026, 1, 2),
        ),
        Paper(
            arxiv_id="2601.10003",
            title="An open dataset for everyday discovery",
            authors=[Author(name="Jordan Researcher")],
            categories=["cs.AI"],
            primary_category="cs.AI",
            published_at=date(2026, 1, 1),
        ),
    ]

    def fixture_result(p):
        kind = "dataset" if p.arxiv_id.endswith("3") else "project_page"
        return PaperResult(
            requested=p.identifier,
            paper=p,
            visuals=[
                VisualCandidate(
                    id="youtube",
                    main_video_score=100 if p.arxiv_id.endswith("1") else 60,
                    media_type="video",
                    delivery="embed",
                    url="https://www.youtube.com/watch?v=abcDEF12345",
                    provider="youtube",
                    provider_id="abcDEF12345",
                    source_page_url="https://example.org/research",
                ),
                VisualCandidate(
                    id="native",
                    main_video_score=60 if p.arxiv_id.endswith("1") else 100,
                    media_type="video",
                    delivery="direct",
                    url="https://example.org/demo.mp4",
                    source_page_url="https://example.org/research",
                ),
            ],
            resources=[
                ResourceLink(url="https://example.org/research", kind=kind),
                ResourceLink(url="https://github.com/example/research", kind="repository"),
            ],
        )

    harvest_calls = 0

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def harvest(self, **kwargs):
            nonlocal harvest_calls
            harvest_calls += 1
            await asyncio.sleep(0.4 if harvest_calls == 1 else 60)
            yield HarvestBatch(
                records=[
                    HarvestRecord(
                        identifier=f"oai:arXiv.org:{p.arxiv_id}",
                        paper=p.model_copy(
                            update={
                                "comments": "https://example.org/research https://github.com/example/research"
                            }
                        ),
                        datestamp=date(2026, 1, 3),
                    )
                    for p in papers
                ]
            )

    async def resolver(p, **kwargs):
        await asyncio.sleep(0.3)
        return fixture_result(p)

    api = create_app(tmp_path / "collection", client_factory=Client, resolver=resolver)
    ready = threading.Event()

    class Server(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets)
            ready.set()

    server = Server(uvicorn.Config(api, log_level="error"))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        assert await asyncio.to_thread(ready.wait, 10)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page(viewport={"width": 1440, "height": 1100})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.route("https://example.org/**", lambda route: route.abort())
            player_headers = []

            async def player_fixture(route):
                player_headers.append(await route.request.all_headers())
                await route.fulfill(
                    status=200, content_type="text/html", body="<p>Offline player fixture</p>"
                )

            await page.route("https://www.youtube-nocookie.com/**", player_fixture)
            await page.goto(f"http://127.0.0.1:{port}")
            # Generate a real, silent video locally to verify browser autoplay offline.
            video_bytes = await page.evaluate("""async () => {
                const canvas = document.createElement('canvas');
                canvas.width = canvas.height = 32;
                const stream = canvas.captureStream(10);
                const recorder = new MediaRecorder(stream, {mimeType: 'video/webm'});
                const chunks = [];
                recorder.ondataavailable = event => chunks.push(event.data);
                const done = new Promise(resolve => recorder.onstop = resolve);
                recorder.start();
                const context = canvas.getContext('2d');
                const timer = setInterval(() => {
                    context.fillStyle = '#555'; context.fillRect(0, 0, 32, 32);
                }, 100);
                await new Promise(resolve => setTimeout(resolve, 1200));
                recorder.stop(); await done; clearInterval(timer);
                stream.getTracks().forEach(track => track.stop());
                return Array.from(new Uint8Array(await new Blob(chunks).arrayBuffer()));
            }""")
            await page.route(
                "https://example.org/demo.mp4",
                lambda route: route.fulfill(content_type="video/webm", body=bytes(video_bytes)),
            )
            await page.get_by_text("Your next discovery starts here.").wait_for()
            await page.get_by_role("button", name="Collect papers", exact=False).click()
            assert await page.locator("#start-button").is_disabled()
            await page.locator("#github-token").fill("github_pat_browser_fixture")
            await page.get_by_role("button", name="Set token", exact=True).click()
            await page.get_by_text(
                "Token saved securely on this computer (not yet verified with GitHub).", exact=True
            ).wait_for()
            assert await page.locator("#github-token").input_value() == ""
            await page.get_by_role("button", name="Clear token", exact=True).click()
            await page.get_by_text(
                "A GitHub token is required before collection can start.", exact=True
            ).wait_for()
            assert await page.locator("#start-button").is_disabled()
            await page.locator("#github-token").fill("github_pat_browser_fixture")
            await page.get_by_role("button", name="Set token", exact=True).click()
            await page.get_by_text("GitHub token (required)", exact=True).click()
            await page.locator("#start").fill("2026-01-01")
            await page.locator("#end").fill("2026-01-03")
            await page.get_by_role("button", name="Start collecting").click()
            await page.get_by_text("Gathering metadata", exact=True).wait_for()
            await page.get_by_role("button", name="Close collection dialog").click()
            await page.locator(".card").nth(1).wait_for()
            await page.get_by_role("button", name="Collect papers", exact=False).click()
            await page.get_by_text("Collection complete", exact=True).wait_for()
            await page.screenshot(path=str(tmp_path / "collection-modal.png"))
            await page.get_by_role("button", name="Close collection dialog").click()
            assert await page.locator(".card").count() == 2
            assert await page.locator("iframe, video").count() == 0
            first = page.locator(".card").first
            await page.mouse.move(0, 0)
            await first.locator("iframe[src]").wait_for(timeout=10000)
            assert (
                await first.locator("iframe").get_attribute("src")
                == "https://www.youtube-nocookie.com/embed/abcDEF12345?autoplay=1&mute=1&playsinline=1"
                + f"&origin=http%3A%2F%2F127.0.0.1%3A{port}"
            )
            await (
                first.locator("iframe")
                .content_frame.get_by_text("Offline player fixture")
                .wait_for()
            )
            assert player_headers[-1]["referer"] == f"http://127.0.0.1:{port}/"
            await first.locator("iframe").evaluate("el => window.savedPlayer = el")
            await page.evaluate("loadGallery()")
            assert await first.locator("iframe").evaluate("el => el === window.savedPlayer")
            await page.get_by_role("searchbox").fill("Learning")
            assert await first.locator("iframe").evaluate("el => el === window.savedPlayer")
            await page.get_by_role("searchbox").fill("")
            await first.get_by_role("button", name="Page", exact=True).click()
            assert await first.locator("iframe").count() == 0
            assert await first.get_by_role("button", name="Play video").count() == 0
            second = page.locator(".card").nth(1)
            await second.get_by_role("button", name="Next visual").click()
            assert await second.locator("video").get_attribute("preload") == "metadata"
            assert await second.locator("video").evaluate(
                "el => el.muted && el.autoplay && el.playsInline"
            )
            await page.wait_for_function("() => document.querySelector('video')?.currentTime > 0")
            await second.get_by_role("button", name="Pause autoplay").click()
            assert await second.locator("video").evaluate("el => el.paused")
            await second.get_by_role("button", name="Resume autoplay").click()
            await second.get_by_role("button", name="Next visual").click()
            assert await first.locator(".list-authors").is_hidden()
            assert await first.locator(".list-abstract").is_hidden()
            await page.get_by_role("button", name="List", exact=True).click()
            assert await first.locator(".list-authors").is_visible()
            assert await first.locator(".list-authors").text_content() == (
                "Alex Researcher, Sam Scientist"
            )
            assert await first.locator(".list-abstract").is_visible()
            assert await first.locator(".list-abstract").text_content() == papers[0].abstract
            assert await page.locator("#gallery").evaluate(
                "el => el.classList.contains('list-view')"
            )
            await page.get_by_role("button", name="Grid", exact=True).click()
            await first.locator(".title-button").click()
            await page.locator("#detail-dialog[open]").wait_for()
            assert await page.locator(".card .authors").count() == 0
            assert (
                await page.locator("#detail-authors").text_content()
                == "Alex Researcher, Sam Scientist"
            )
            assert await page.locator("#detail-dialog :is(img, video, iframe, nav)").count() == 0
            await page.locator("#detail-title").click()
            assert await page.locator("#detail-dialog").evaluate("el => el.open")
            await page.screenshot(path=str(tmp_path / "detail-desktop.png"), animations="disabled")
            await page.mouse.click(5, 5)
            assert not await page.locator("#detail-dialog").evaluate("el => el.open")
            await page.screenshot(
                path=str(tmp_path / "gallery-desktop.png"), full_page=True, animations="disabled"
            )
            await page.get_by_role("button", name="Gallery filters", exact=True).click()
            await page.get_by_role("button", name="Datasets", exact=True).click()
            await page.get_by_role("button", name="Close gallery filters").click()
            assert await page.locator(".card").count() == 1
            await page.get_by_role("button", name="Gallery filters", exact=True).click()
            await page.get_by_role("button", name="All papers", exact=False).click()
            await page.get_by_role("button", name="Close gallery filters").click()
            await page.get_by_role("button", name="Gallery filters", exact=True).click()
            await page.locator("#filter-category").select_option("cs.AI")
            assert await page.locator(".card").count() == 2
            await page.locator("#filter-from").fill("2026-01-03")
            await page.locator("#filter-through").fill("2026-01-03")
            assert await page.locator(".card").count() == 1
            await page.locator("#filter-from").fill("2026-01-04")
            assert await page.locator("#filter-error").is_visible()
            assert await page.locator(".card").count() == 1
            await page.get_by_role("button", name="Clear filters", exact=True).click()
            assert await page.locator(".card").count() == 3
            assert await page.locator("#filter-badge").is_hidden()
            await page.keyboard.press("Escape")
            assert not await page.locator("#gallery-filters").is_visible()
            await page.get_by_role("searchbox").fill("world in motion")
            assert await page.locator(".card").count() == 1
            await page.locator(".title-button").click()
            await page.locator("#detail-dialog[open]").wait_for()
            assert await page.locator("#detail-abstract").text_content() == papers[0].abstract
            assert await page.locator("#detail-resources a").count() == 3
            await page.keyboard.press("Escape")
            key = await page.locator(".card").first.get_attribute("data-key")
            detail_url = f"http://127.0.0.1:{port}/api/papers/{key}"
            detail = await (await page.request.get(detail_url)).json()
            detail["result"]["resources"].extend(
                [
                    {"kind": "paper", "url": "https://arxiv.org/abs/2601.10001"},
                    {"kind": "paper", "url": "http://arxiv.org/pdf/2601.10001v2.pdf"},
                    {"kind": "paper", "url": "https://arxiv.org/html/2601.10001v3#intro"},
                    {"kind": "paper", "url": "https://arxiv.org/abs/2601.99999"},
                    {"kind": "repository", "url": "https://github.com/example/research"},
                    {"kind": "repository", "url": "https://github.com/example/other"},
                ]
            )
            await page.route(detail_url, lambda route: route.fulfill(json=detail), times=1)
            await page.locator(".title-button").click()
            await page.locator("#detail-dialog[open]").wait_for()
            assert await page.locator("#detail-resources a.paper").count() == 2
            assert await page.locator("#detail-resources a.repository").count() == 2
            assert await page.locator("#detail-resources a").count() == 5
            assert await page.locator("#detail-resources a.paper").first.get_attribute("href") == (
                "https://arxiv.org/abs/2601.10001"
            )
            await page.keyboard.press("Escape")
            await page.get_by_role("searchbox").fill("")
            await page.set_viewport_size({"width": 390, "height": 844})
            await page.screenshot(
                path=str(tmp_path / "gallery-mobile.png"), full_page=True, animations="disabled"
            )
            await page.get_by_role("button", name="Gallery filters", exact=True).click()
            assert await page.locator("#gallery-filters").evaluate(
                "el => el.scrollWidth <= el.clientWidth"
            )
            await page.mouse.click(5, 5)
            assert not await page.locator("#gallery-filters").is_visible()
            await page.get_by_role("button", name="List", exact=True).click()
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            await page.get_by_role("button", name="Grid", exact=True).click()
            await page.locator(".title-button").first.click()
            await page.locator("#detail-dialog[open]").wait_for()
            assert await page.locator("#detail-dialog").evaluate(
                "el => el.scrollWidth <= el.clientWidth"
            )
            await page.screenshot(path=str(tmp_path / "detail-mobile.png"), animations="disabled")
            await page.keyboard.press("Escape")
            await page.get_by_role("searchbox").fill("no-such-paper")
            await page.get_by_role("button", name="Reset filters").click()
            assert await page.locator(".card").count() == 3
            await page.emulate_media(reduced_motion="reduce")
            assert (
                await page.locator(".card").first.evaluate(
                    "el => getComputedStyle(el).animationName"
                )
                == "none"
            )
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            await page.get_by_role("button", name="Collect papers", exact=False).click()
            assert await page.locator("#collect-dialog").evaluate(
                "el => el.scrollWidth <= el.clientWidth"
            )
            await page.get_by_role("button", name="Start collecting").click()
            await page.get_by_text("Gathering metadata", exact=True).wait_for()
            assert await page.locator("#collect-fields").is_hidden()
            await page.screenshot(path=str(tmp_path / "collection-running-mobile.png"))
            await page.get_by_role("button", name="Stop collection", exact=True).click()
            await page.get_by_text("Collection stopped", exact=True).wait_for()
            assert await page.locator("#stop-button").is_hidden()
            assert await page.locator("#collect-fields").is_visible()
            assert await page.locator(".card").count() == 3
            await page.reload()
            await page.locator(".card").nth(1).wait_for()
            assert await page.locator(".card").count() == 2
            assert not errors
            response = await page.request.get(f"http://127.0.0.1:{port}/api/gallery")
            payload = await response.json()
            template = payload["items"][0]
            items = []
            for index in range(43):
                item = copy.deepcopy(template)
                item["key"] = f"fixture-{index}"
                item["paper"]["title"] = f"Pagination fixture {index:02d}"
                items.append(item)
            await page.route(
                "**/api/gallery",
                lambda route: route.fulfill(json={"items": items, "saved": len(items)}),
            )
            await page.set_viewport_size({"width": 1440, "height": 900})
            await page.reload()
            await page.locator(".card").nth(17).wait_for()
            assert await page.locator(".card").count() == 18
            assert await page.locator("footer").count() == 0
            await page.locator(".card").first.evaluate("el => window.savedCard = el")
            await page.locator("#load-more").scroll_into_view_if_needed()
            await page.locator(".card").nth(35).wait_for()
            assert await page.locator(".card").first.evaluate("el => el === window.savedCard")
            await page.locator("#load-more").scroll_into_view_if_needed()
            await page.locator(".card").nth(42).wait_for()
            assert await page.locator(".card").count() == 43
            assert await page.locator("#gallery-pagination").is_hidden()
            await page.get_by_role("searchbox").fill("fixture 42")
            assert await page.locator(".card").count() == 1
            await page.get_by_role("searchbox").fill("")
            assert await page.locator(".card").count() == 18
            await page.get_by_role("button", name="Gallery filters", exact=True).click()
            await page.get_by_role("button", name="Datasets", exact=True).click()
            await page.get_by_role("button", name="Close gallery filters").click()
            assert await page.locator(".card").count() == 0
            assert await page.locator("#gallery-pagination").is_hidden()
            assert not errors
            await browser.close()
        assert len(Store(tmp_path / "collection").gallery()["items"]) == 3
        print(f"Browser screenshots: {tmp_path}")
    finally:
        server.should_exit = True
        await asyncio.to_thread(thread.join, 5)
        sock.close()
