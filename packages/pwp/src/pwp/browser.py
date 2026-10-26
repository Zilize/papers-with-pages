"""Caller-owned, lazy Chromium reuse with isolated and bounded contexts."""

import asyncio
from contextlib import asynccontextmanager, suppress

from pwp.errors import PwpError

CONTEXT_SETUP_TIMEOUT = 30
CONTEXT_CLEANUP_TIMEOUT = 5


async def _reject_proxy(reader, writer):
    try:
        writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()
    except ConnectionError:
        pass
    finally:
        writer.close()


class RenderSession:
    """Reuse one browser on one event loop; each lease owns a fresh context.

    Close after cancelling or awaiting callers. Disconnected browsers are rebuilt
    on the next lease; interrupted pages are not silently retried.
    """

    def __init__(self, *, concurrency=2):
        if not isinstance(concurrency, int) or not 1 <= concurrency <= 16:
            raise ValueError("Render concurrency must be between 1 and 16")
        self.concurrency = concurrency
        self._slots = asyncio.Semaphore(concurrency)
        self._lock = asyncio.Lock()
        self._browser = None
        self._playwright = None
        self._proxy = None
        self._closed = False

    async def __aenter__(self):
        if self._closed:
            raise RuntimeError("RenderSession is closed")
        return self

    async def __aexit__(self, *args):
        await self.aclose()

    async def _ensure_browser(self):
        async with self._lock:
            if self._closed:
                raise RuntimeError("RenderSession is closed")
            if self._browser is not None and self._browser.is_connected():
                return self._browser
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:
                raise PwpError(
                    "render_unavailable", "Install the render extra and Chromium"
                ) from exc
            try:
                if self._playwright is None:
                    self._playwright = await async_playwright().start()
                if self._proxy is None:
                    self._proxy = await asyncio.start_server(_reject_proxy, "127.0.0.1", 0)
                port = self._proxy.sockets[0].getsockname()[1]
                self._browser = await self._playwright.chromium.launch(
                    headless=True,
                    proxy={"server": f"http://127.0.0.1:{port}"},
                    args=[
                        "--proxy-bypass-list=<-loopback>",
                        "--disable-quic",
                        "--disable-background-networking",
                        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                    ],
                )
                return self._browser
            except BaseException:
                await self._dispose()
                raise

    @asynccontextmanager
    async def context(self, *, viewport, scale):
        async with self._slots:
            async with asyncio.timeout(CONTEXT_SETUP_TIMEOUT):
                browser = await self._ensure_browser()
                context = await browser.new_context(
                    viewport={"width": viewport[0], "height": viewport[1]},
                    device_scale_factor=scale,
                    service_workers="block",
                    accept_downloads=False,
                    reduced_motion="reduce",
                )
            try:
                yield context
            finally:
                from playwright.async_api import Error

                # Cancelling unroute_all(wait) cancels Playwright's invocation futures.
                # A late handler then raises InvalidStateError in the shared driver.
                # Stop dispatching first; do not wait on those internal futures.
                with suppress(Error, TimeoutError):
                    async with asyncio.timeout(CONTEXT_CLEANUP_TIMEOUT):
                        await context.unroute_all(behavior="ignoreErrors")
                with suppress(Error, TimeoutError):
                    async with asyncio.timeout(CONTEXT_CLEANUP_TIMEOUT):
                        await context.close()

    async def _dispose(self):
        try:
            if self._browser is not None:
                with suppress(Exception):
                    async with asyncio.timeout(CONTEXT_CLEANUP_TIMEOUT):
                        await self._browser.close()
        finally:
            self._browser = None
            try:
                if self._playwright is not None:
                    with suppress(Exception):
                        async with asyncio.timeout(CONTEXT_CLEANUP_TIMEOUT):
                            await self._playwright.stop()
            finally:
                self._playwright = None
                if self._proxy is not None:
                    self._proxy.close()
                    await self._proxy.wait_closed()
                    self._proxy = None

    async def aclose(self):
        self._closed = True
        acquired = 0
        try:
            for _ in range(self.concurrency):
                await self._slots.acquire()
                acquired += 1
            async with self._lock:
                await self._dispose()
        finally:
            for _ in range(acquired):
                self._slots.release()
