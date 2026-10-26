"""Bounded HTTP fetching with DNS validation at the actual connection boundary."""

import asyncio
import codecs
import ipaddress
import math
import re
import socket
import time
from collections import OrderedDict
from collections.abc import Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from urllib.parse import urldefrag, urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import aiohttp

from pwp.errors import RequestError
from pwp.extract import normalize_url
from pwp.harvest.http import retry_after_seconds


def public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped:
            return public_address(str(address.ipv4_mapped))
        if address.sixtofour or address.teredo or address in ipaddress.ip_network("64:ff9b::/96"):
            return False
    return address.is_global and not address.is_multicast


def validate_target(url: str) -> str:
    """Validate URL shape and literal addresses; DNS is checked by PublicResolver."""
    try:
        url = urldefrag(normalize_url(url))[0]
        parts = urlsplit(url)
        host = (parts.hostname or "").rstrip(".")
        if parts.port not in {None, 80, 443}:
            raise ValueError("Only HTTP(S) default ports are supported")
        if host in {"localhost", "localhost.localdomain"} or host.endswith(
            (".localhost", ".local")
        ):
            raise ValueError("Local hostnames are not allowed")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            if not public_address(host):
                raise ValueError("Non-public addresses are not allowed")
        if host == "arxiv.org" or host.endswith(".arxiv.org"):
            raise ValueError("Use the metadata client; paper page fetching is not supported")
        if parts.path.lower().endswith((".pdf", ".ps", ".ps.gz")):
            raise ValueError("Paper document downloads are not supported")
        return url
    except ValueError as exc:
        raise RequestError("blocked_url", str(exc)) from exc


class UnsafeAddressError(OSError):
    """A hostname resolved outside the public network policy."""


def validate_proxy(value: str | None) -> str | None:
    """Accept an explicit HTTP proxy at a loopback or public numeric endpoint.

    Through a proxy, target hostnames are resolved by the proxy, so PublicResolver
    cannot check them; only URL shapes and literal addresses are validated locally.
    A trusted proxy must therefore enforce its own private-network egress policy.
    """
    if value is None:
        return None
    try:
        parts = urlsplit(value)
        address = ipaddress.ip_address(parts.hostname or "")
        if (
            parts.scheme != "http"
            or parts.username is not None
            or parts.password is not None
            or parts.path not in {"", "/"}
            or parts.query
            or parts.fragment
            or not parts.port
            or not (address.is_loopback or public_address(str(address)))
        ):
            raise ValueError
    except ValueError as exc:
        raise ValueError(
            "Trusted proxy must be http://<loopback-or-public-IP>:<port> without credentials"
        ) from exc
    return value.rstrip("/")


class PublicResolver(aiohttp.abc.AbstractResolver):
    """Pass only validated numeric addresses to the connector; never resolve twice."""

    def __init__(self):
        self.delegate = aiohttp.resolver.ThreadedResolver()

    async def resolve(self, host, port=0, family=socket.AF_INET):
        results = await self.delegate.resolve(host, port, family)
        if not results or any(not public_address(item["host"]) for item in results):
            raise UnsafeAddressError("DNS resolved to a non-public address")
        return results

    async def close(self):
        await self.delegate.close()


@dataclass
class RequestBudget:
    maximum: int | None = 80
    used: int = 0

    def consume(self) -> None:
        if self.maximum is not None and self.used >= self.maximum:
            raise RequestError("request_limit", "The request budget is exhausted")
        self.used += 1


_scope_budget: ContextVar[tuple[RequestBudget, ...]] = ContextVar("pwp_request_budget", default=())


@contextmanager
def request_scope(budget: RequestBudget) -> Iterator[None]:
    token = _scope_budget.set((*_scope_budget.get(), budget))
    try:
        yield
    finally:
        _scope_budget.reset(token)


def consume_request(budget: RequestBudget) -> None:
    for scoped in _scope_budget.get():
        if scoped is not budget:
            scoped.consume()
    budget.consume()


_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_.:-]+)""", re.I)


def _charset(content_type: str, body: bytes) -> str:
    """Prefer the HTTP charset, then a byte-order mark, then an early HTML declaration."""
    declared = content_type.partition("charset=")[2].split(";")[0].strip(' "')
    if declared:
        return declared
    for mark, name in (
        (codecs.BOM_UTF8, "utf-8-sig"),
        (codecs.BOM_UTF16_LE, "utf-16"),
        (codecs.BOM_UTF16_BE, "utf-16"),
    ):
        if body.startswith(mark):
            return name
    match = _META_CHARSET.search(body[:4096])
    return match.group(1).decode("ascii") if match else "utf-8"


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int
    headers: dict[str, str]
    body: bytes
    history: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        charset = _charset(self.headers.get("content-type", ""), self.body)
        try:
            return self.body.decode(charset, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")


class SafeFetcher:
    """No ambient proxies, credentials, cookies, or unvalidated redirect connections.

    Requests made with ``asset_budget`` are browser subresources of an already
    admitted page: they skip robots checks and crawl pacing and consume only that
    budget. URL and DNS validation, redirects, byte limits, and server backoff apply.
    """

    # Idle per-host state is pruned beyond this many hosts to keep reuse bounded.
    MAX_TRACKED_HOSTS = 1024

    def __init__(
        self,
        *,
        budget: RequestBudget | None = None,
        timeout: float = 20,
        max_bytes: int = 8 * 1024 * 1024,
        max_redirects: int = 5,
        min_interval: float = 0.25,
        user_agent: str = "papers-with-pages/0.1",
        respect_robots: bool = True,
        max_retries: int = 1,
        trusted_proxy: str | None = None,
        max_connections: int = 4,
    ):
        if (
            not math.isfinite(timeout)
            or not math.isfinite(min_interval)
            or timeout <= 0
            or max_bytes < 1
            or max_redirects < 0
            or min_interval < 0
            or not 0 <= max_retries <= 5
            or not isinstance(max_connections, int)
            or not 1 <= max_connections <= 32
        ):
            raise ValueError("Invalid network limits")
        self.budget = budget if budget is not None else RequestBudget()
        self.trusted_proxy = validate_proxy(trusted_proxy)
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self.min_interval = min_interval
        self.user_agent = user_agent
        self.respect_robots = respect_robots
        self.max_retries = max_retries
        self.max_connections = max_connections
        self._session = None
        self._github_session = None
        self._resolver = None
        self._metrics = {
            lane: {
                "requests": 0,
                "completed": 0,
                "failed": 0,
                "cancelled": 0,
                "bytes": 0,
                "retries": 0,
                "active": 0,
                "host_queue_active": 0,
                "host_queue_s": 0.0,
                "pacing_active": 0,
                "pacing_s": 0.0,
                "network_active": 0,
                "network_s": 0.0,
                "connector_queue_s": 0.0,
            }
            for lane in ("web", "github")
        }
        self._locks: dict[str, asyncio.Lock] = {}
        self._host_slots: dict[str, asyncio.Semaphore] = {}
        self._github_until = 0.0
        self._github_quota = {"limit": None, "remaining": None, "reset_at": None, "status": None}
        self._next: dict[str, float] = {}
        self._robots: OrderedDict[str, RobotFileParser] = OrderedDict()
        self._robots_locks: dict[str, asyncio.Lock] = {}
        self._robots_retry_at: dict[str, float] = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.aclose()

    async def aclose(self):
        if self._github_session is not None:
            await self._github_session.close()
            self._github_session = None
        if self._session is not None:
            await self._session.close()
            self._session = None
        if self._resolver is not None:
            await self._resolver.close()
            self._resolver = None

    def statistics(self):
        """Bounded aggregate timings; never retain URLs, headers, or credentials.

        Durations sum concurrent request time, not elapsed wall time. Connector
        queue time is a subset of network time. Active phases include ongoing waits.
        """
        snapshot = {lane: dict(values) for lane, values in self._metrics.items()}
        snapshot["github"]["quota"] = dict(self._github_quota)
        snapshot["github"]["cooldown_remaining_s"] = round(
            max(0, self._github_until - time.monotonic()), 3
        )
        return snapshot

    @contextmanager
    def _phase(self, metrics, phase):
        started = time.perf_counter()
        metrics[phase + "_active"] += 1
        try:
            yield
        finally:
            metrics[phase + "_active"] -= 1
            metrics[phase + "_s"] += time.perf_counter() - started

    @asynccontextmanager
    async def _host_slot(self, slots, metrics):
        with self._phase(metrics, "host_queue"):
            await slots.acquire()
        try:
            yield
        finally:
            slots.release()

    async def _get_session(self, *, github=False):
        name = "_github_session" if github else "_session"
        if getattr(self, name) is None:
            if self._resolver is None:
                self._resolver = PublicResolver()
            metrics = self._metrics["github" if github else "web"]
            trace = aiohttp.TraceConfig()

            async def queue_start(session, context, params):
                context.queued_at = time.perf_counter()

            async def queue_end(session, context, params):
                if hasattr(context, "queued_at"):
                    metrics["connector_queue_s"] += time.perf_counter() - context.queued_at
                    del context.queued_at

            trace.on_connection_queued_start.append(queue_start)
            trace.on_connection_queued_end.append(queue_end)
            trace.on_request_exception.append(queue_end)
            session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(
                    resolver=self._resolver,
                    limit=1 if github else self.max_connections,
                    limit_per_host=1 if github else 4,
                ),
                trust_env=False,
                cookie_jar=aiohttp.DummyCookieJar(),
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                trace_configs=[trace],
            )
            setattr(self, name, session)
        return getattr(self, name)

    @staticmethod
    def _host_capacity(host: str) -> int:
        return 1 if host == "api.github.com" else 4

    def _prune_hosts(self) -> None:
        """Drop idle per-host state; entries in use or still in backoff are kept."""
        now = time.monotonic()
        if len(self._locks) > self.MAX_TRACKED_HOSTS:
            for host in list(self._locks):
                slots = self._host_slots.get(host)
                idle = slots is None or (
                    not slots.locked() and slots._value == self._host_capacity(host)
                )
                if idle and not self._locks[host].locked() and self._next.get(host, 0) <= now:
                    del self._locks[host]
                    self._host_slots.pop(host, None)
                    self._next.pop(host, None)
        if len(self._robots_locks) > self.MAX_TRACKED_HOSTS:
            for origin in list(self._robots_locks):
                if (
                    not self._robots_locks[origin].locked()
                    and self._robots_retry_at.get(origin, 0) <= now
                ):
                    del self._robots_locks[origin]
                    self._robots_retry_at.pop(origin, None)
        while len(self._robots) > self.MAX_TRACKED_HOSTS:
            self._robots.popitem(last=False)

    async def _request(
        self,
        url: str,
        headers: Mapping[str, str],
        max_bytes: int,
        *,
        omit_video: bool = False,
        asset_budget: RequestBudget | None = None,
    ) -> FetchResult:
        metrics = self._metrics["github" if urlsplit(url).netloc == "api.github.com" else "web"]
        metrics["requests"] += 1
        metrics["active"] += 1
        try:
            result = await self._request_measured(
                url, headers, max_bytes, metrics, omit_video, asset_budget
            )
            metrics["completed"] += 1
            metrics["bytes"] += len(result.body)
            return result
        except asyncio.CancelledError:
            metrics["cancelled"] += 1
            raise
        except Exception:
            metrics["failed"] += 1
            raise
        finally:
            metrics["active"] -= 1

    async def _request_measured(self, url, headers, max_bytes, metrics, omit_video, asset_budget):
        host = urlsplit(url).netloc
        # No await separates pruning from setdefault, so pruned state is never in use.
        self._prune_hosts()
        lock = self._locks.setdefault(host, asyncio.Lock())
        slots = self._host_slots.setdefault(host, asyncio.Semaphore(self._host_capacity(host)))
        async with self._host_slot(slots, metrics):
            with self._phase(metrics, "pacing"):
                await self._pace(host, lock, asset_budget)
            session = (
                await self._get_session(github=True)
                if host == "api.github.com"
                else await self._get_session()
            )
            with self._phase(metrics, "network"):
                return await self._read_response(session, url, headers, max_bytes, omit_video)

    async def _pace(
        self, host: str, lock: asyncio.Lock, asset_budget: RequestBudget | None = None
    ) -> None:
        async with lock:
            if host == "api.github.com" and self._github_until > time.monotonic():
                raise RequestError(
                    "rate_limited",
                    "GitHub API is cooling down",
                    retryable=True,
                    retry_after_s=self._github_until - time.monotonic(),
                )
            wait = max(0, self._next.get(host, 0) - time.monotonic())
            if wait > self.timeout:
                raise RequestError(
                    "backoff_active", "Host is in backoff", retryable=True, retry_after_s=wait
                )
            await asyncio.sleep(wait)
            if asset_budget is not None:
                # Subresources honor server backoff but add no crawl pacing.
                asset_budget.consume()
                return
            consume_request(self.budget)
            self._next[host] = time.monotonic() + self.min_interval

    async def _read_response(self, session, url, headers, max_bytes, omit_video):
        host = urlsplit(url).netloc
        try:
            options = {"proxy": self.trusted_proxy} if self.trusted_proxy else {}
            async with session.get(
                url, headers=headers, allow_redirects=False, **options
            ) as response:
                if host == "api.github.com":
                    self._github_quota["status"] = response.status
                    for key, header in (
                        ("limit", "X-RateLimit-Limit"),
                        ("remaining", "X-RateLimit-Remaining"),
                        ("reset_at", "X-RateLimit-Reset"),
                    ):
                        try:
                            self._github_quota[key] = max(0, int(response.headers[header]))
                        except (KeyError, ValueError, TypeError):
                            self._github_quota[key] = None
                    delay = github_cooldown(response.status, response.headers)
                    if delay:
                        self._github_until = max(self._github_until, time.monotonic() + delay)
                content_type = response.headers.get("Content-Type", "").lower()
                if "application/pdf" in content_type or "application/postscript" in content_type:
                    raise RequestError(
                        "document_blocked", "Paper document downloads are not supported"
                    )
                if omit_video and content_type.startswith("video/"):
                    return FetchResult(
                        url,
                        str(response.url),
                        response.status,
                        {k.lower(): v for k, v in response.headers.items()},
                        b"",
                    )
                delay = retry_after_seconds(response.headers.get("Retry-After"))
                if delay is not None:
                    self._next[host] = max(self._next.get(host, 0), time.monotonic() + delay)
                data = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    if len(data) + len(chunk) > max_bytes:
                        raise RequestError("response_too_large", "Response exceeded byte limit")
                    data.extend(chunk)
                return FetchResult(
                    url,
                    str(response.url),
                    response.status,
                    {k.lower(): v for k, v in response.headers.items()},
                    bytes(data),
                )
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            if isinstance(getattr(exc, "os_error", exc), UnsafeAddressError):
                raise RequestError("blocked_dns", "DNS resolved to a non-public address") from exc
            raise RequestError("fetch_failed", "HTTP request failed", retryable=True) from exc

    async def _check_robots(self, url: str) -> None:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        async with self._robots_locks.setdefault(origin, asyncio.Lock()):
            remaining = self._robots_retry_at.get(origin, 0) - time.monotonic()
            if remaining > 0:
                raise RequestError(
                    "robots_unavailable",
                    "Robots policy temporarily unavailable",
                    retryable=True,
                    retry_after_s=remaining,
                )
            if origin not in self._robots:
                try:
                    response = await self.get(
                        origin + "/robots.txt", check_robots=False, max_bytes=512_000
                    )
                except RequestError as exc:
                    if not exc.issue.retryable or exc.issue.code in {
                        "request_limit",
                        "rate_limited",
                        "backoff_active",
                    }:
                        raise
                    # Back off connection failures too, instead of refetching per URL.
                    self._robots_retry_at[origin] = time.monotonic() + 30
                    raise RequestError(
                        "robots_unavailable",
                        "Cannot retrieve robots policy",
                        retryable=True,
                        retry_after_s=30,
                    ) from exc
                parser = RobotFileParser()
                if response.status in {401, 403}:
                    parser.parse(["User-agent: *", "Disallow: /"])
                elif response.status == 404:
                    parser.parse([])
                elif response.status >= 400:
                    self._robots_retry_at[origin] = max(
                        time.monotonic() + 30, self._next.get(parts.netloc, 0)
                    )
                    raise RequestError(
                        "robots_unavailable",
                        "Cannot retrieve robots policy",
                        retryable=True,
                        retry_after_s=self._robots_retry_at[origin] - time.monotonic(),
                    )
                else:
                    parser.parse(response.text.splitlines())
                self._robots[origin] = parser
            self._robots.move_to_end(origin)
        policy = self._robots[origin]
        if not policy.can_fetch(self.user_agent, url):
            raise RequestError("robots_denied", "The robots policy disallows this URL")
        delay = policy.crawl_delay(self.user_agent)
        if delay:
            self._next[parts.netloc] = max(
                self._next.get(parts.netloc, 0), time.monotonic() + delay
            )

    async def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        max_bytes: int | None = None,
        check_robots: bool = True,
        omit_video: bool = False,
        asset_budget: RequestBudget | None = None,
    ) -> FetchResult:
        """Fetch one URL; ``asset_budget`` marks a browser subresource (see class docs)."""
        original = validate_target(url)
        if max_bytes is not None and max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        current = original
        headers = {"User-Agent": self.user_agent, **(headers or {})}
        history = []
        for _ in range(self.max_redirects + 1):
            current = validate_target(current)
            if check_robots and self.respect_robots and asset_budget is None:
                await self._check_robots(current)
            for attempt in range(self.max_retries + 1):
                try:
                    options = {"omit_video": True} if omit_video else {}
                    if asset_budget is not None:
                        options["asset_budget"] = asset_budget
                    response = await self._request(
                        current, headers, max_bytes or self.max_bytes, **options
                    )
                except RequestError as exc:
                    if (
                        not exc.issue.retryable
                        or attempt == self.max_retries
                        or exc.issue.code in {"rate_limited", "backoff_active"}
                    ):
                        raise
                else:
                    if (
                        response.status not in {429, 500, 502, 503, 504}
                        or attempt == self.max_retries
                    ):
                        break
                host = urlsplit(current).netloc
                self._metrics["github" if host == "api.github.com" else "web"]["retries"] += 1
                self._next[host] = max(self._next.get(host, 0), time.monotonic() + 2**attempt)
            if response.status in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    raise RequestError("invalid_redirect", "Redirect has no Location header")
                target = validate_target(urljoin(current, location))
                if target in history or target == current:
                    raise RequestError("redirect_loop", "Redirect loop detected")
                if (urlsplit(target).scheme, urlsplit(target).netloc) != (
                    urlsplit(current).scheme,
                    urlsplit(current).netloc,
                ):
                    headers = {"User-Agent": self.user_agent}
                history.append(current)
                current = target
                continue
            response.url = original
            response.history = history
            return response
        raise RequestError("redirect_limit", "Too many redirects")


def require_success(response: FetchResult) -> None:
    if not 200 <= response.status < 300:
        if response.status in {403, 429} and (
            response.headers.get("x-ratelimit-remaining") == "0"
            or response.status == 429
            or response.headers.get("retry-after") is not None
        ):
            raise RequestError(
                "rate_limited",
                "Upstream API request limit reached",
                retryable=True,
                retry_after_s=github_cooldown(response.status, response.headers),
                http_status=response.status,
            )
        raise RequestError(
            "http_error",
            f"HTTP {response.status}",
            retryable=response.status in {429, 500, 502, 503, 504},
            http_status=response.status,
        )


def github_cooldown(status: int, headers: Mapping[str, str]) -> float:
    """Convert quota reset headers to a bounded delay without retaining credentials."""
    headers = {k.lower(): v for k, v in headers.items()}
    exhausted = headers.get("x-ratelimit-remaining") == "0"
    if not exhausted and status != 429 and not (status == 403 and "retry-after" in headers):
        return 0
    delays = [retry_after_seconds(headers.get("retry-after")) or 0]
    if exhausted:
        try:
            delays.append(float(headers.get("x-ratelimit-reset", "")) - time.time())
        except (ValueError, TypeError):
            pass
    return min(86400, max(1, *delays)) if max(delays) > 0 else 60
