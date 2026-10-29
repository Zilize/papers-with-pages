"""Serve the local gallery and its collection modal."""

import asyncio
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from filelock import FileLock
from pwp.errors import PwpError
from pwp.network import validate_proxy
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.credentials import TokenStore, TokenStoreError
from app.jobs import Collector, HarvestRequest
from app.storage import Store, write_thumbnail

STATIC = Path(__file__).parent / "static"


def create_app(
    data_dir: Path | str = "data",
    *,
    client_factory=None,
    resolver=None,
    trusted_proxy=None,
    github_token=None,
    max_workers=8,
    render_pages=6,
):
    trusted_proxy = validate_proxy(trusted_proxy)
    store = Store(Path(data_dir))
    gallery_lock = asyncio.Lock()
    gallery_cache = {}
    credentials = TokenStore(store.root)
    settings_lock = asyncio.Lock()
    dependencies = {
        "trusted_proxy": trusted_proxy,
        "max_workers": max_workers,
        "render_pages": render_pages,
        "github_token": github_token,
    }
    if client_factory is not None:
        dependencies["client_factory"] = client_factory
    if resolver is not None:
        dependencies["resolver"] = resolver

    @asynccontextmanager
    async def lifespan(api):
        lock = FileLock(store.root / ".app.lock")
        lock.acquire(timeout=0)
        try:
            store.recover_runs()
            api.state.collector = Collector(store, **dependencies)
            api.state.token_persisted = False
            api.state.token_storage_error = None
            try:
                saved = await asyncio.to_thread(credentials.load)
                if saved:
                    api.state.collector.github_token = saved
                    api.state.token_persisted = True
            except TokenStoreError as exc:
                api.state.token_storage_error = str(exc)
            yield
        finally:
            if hasattr(api.state, "collector"):
                await api.state.collector.close()
            lock.release()

    api = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    api.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"]
    )

    @api.middleware("http")
    async def protect_local_app(request: Request, call_next):
        if request.method not in {"GET", "HEAD"}:
            origin = request.headers.get("origin")
            if (
                request.headers.get("x-pwp-app") != "1"
                or (origin and origin != str(request.base_url).rstrip("/"))
                or request.headers.get("sec-fetch-site") == "cross-site"
            ):
                return JSONResponse(
                    {"detail": "Use the local gallery to start a collection"}, status_code=403
                )
        response = await call_next(request)
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
            "connect-src 'self'; object-src 'none'; media-src 'self' https:; "
            "frame-src https://www.youtube-nocookie.com https://player.vimeo.com; base-uri 'none'; "
            "frame-ancestors 'none'; form-action 'self'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @api.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @api.get("/api/gallery")
    async def gallery():
        async with gallery_lock:
            state = api.state.collector.state or {}
            revision = tuple(state.get(k) for k in ("id", "harvested", "completed", "status"))
            try:
                revision += ((store.root / "papers").stat().st_mtime_ns,)
            except FileNotFoundError:
                revision += (None,)
            if gallery_cache.get("revision") != revision or time.monotonic() >= gallery_cache.get(
                "until", 0
            ):
                try:
                    gallery_cache["payload"] = await asyncio.to_thread(store.gallery)
                except PermissionError:
                    raise HTTPException(
                        503,
                        "Collection files are temporarily locked; retry shortly",
                        headers={"Retry-After": "2"},
                    ) from None
                gallery_cache["revision"] = revision
                gallery_cache["until"] = time.monotonic() + 2
            return gallery_cache["payload"]

    @api.get("/api/papers/{key}")
    async def detail(key: str):
        if not key or any(c not in "0123456789abcdef" for c in key):
            raise HTTPException(404, "Paper not found")
        try:
            result = await asyncio.to_thread(store.detail, key)
        except PermissionError:
            raise HTTPException(
                503,
                "Paper files are temporarily locked; retry shortly",
                headers={"Retry-After": "2"},
            ) from None
        if result is None:
            raise HTTPException(404, "Paper not found")
        return result

    @api.get("/api/collection")
    async def status():
        collector = api.state.collector
        if collector.task is None or collector.task.done():
            collector.state = store.latest_run()
        return collector.state

    @api.get("/api/settings/github")
    async def github_settings():
        return {
            "configured": bool(api.state.collector.github_token),
            "persisted": api.state.token_persisted,
            "storage_error": api.state.token_storage_error,
        }

    @api.put("/api/settings/github")
    async def update_github_settings(request: Request):
        collector = api.state.collector
        if collector.task is not None and not collector.task.done():
            raise HTTPException(409, "Wait for collection to finish before changing credentials")
        # Parse manually so validation errors never echo a submitted credential.
        try:
            payload = await request.json()
        except ValueError:
            raise HTTPException(400, "Expected a token configuration") from None
        token = payload.get("token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or (
            token and not re.fullmatch(r"[A-Za-z0-9_]{8,512}", token)
        ):
            raise HTTPException(400, "Enter a valid token, or clear the configuration")
        async with settings_lock:
            if collector.task is not None and not collector.task.done():
                raise HTTPException(
                    409, "Wait for collection to finish before changing credentials"
                )
            try:
                await asyncio.to_thread(credentials.save, token)
            except TokenStoreError as exc:
                raise HTTPException(503, str(exc)) from None
            collector.github_token = token or None
            api.state.token_persisted = bool(token)
            api.state.token_storage_error = None
        return await github_settings()

    @api.post("/api/collection", status_code=202)
    async def start(request: HarvestRequest):
        async with settings_lock:
            if not api.state.collector.github_token:
                raise HTTPException(400, "Configure a GitHub token before starting collection")
            try:
                return api.state.collector.start(request)
            except RuntimeError as exc:
                raise HTTPException(409, str(exc)) from exc

    @api.post("/api/collection/stop")
    async def stop():
        return await api.state.collector.close()

    @api.get("/thumbnails/{path:path}")
    async def thumbnail(path: str):
        assets = store.root / "assets"
        source = (assets / path).resolve()
        if (
            not source.is_relative_to(assets)
            or source.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}
            or not source.is_file()
        ):
            raise HTTPException(404, "Image not found")
        try:
            target = await asyncio.to_thread(write_thumbnail, source)
        except (OSError, PwpError):
            raise HTTPException(404, "Thumbnail unavailable") from None
        # Sources are content-addressed, so a thumbnail never changes for its URL.
        return FileResponse(
            target,
            media_type="image/webp",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    api.mount("/static", StaticFiles(directory=STATIC), name="static")
    api.mount("/assets", StaticFiles(directory=store.root / "assets"), name="assets")
    return api
