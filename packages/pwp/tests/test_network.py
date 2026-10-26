import asyncio
import socket
import time

import pytest
from pwp.errors import RequestError
from pwp.network import (
    FetchResult,
    PublicResolver,
    RequestBudget,
    SafeFetcher,
    public_address,
    request_scope,
    validate_proxy,
    validate_target,
)


async def test_github_quota_cools_down_without_extra_requests(monkeypatch):
    from pwp.network import github_cooldown, require_success

    response = FetchResult(
        "https://api.github.com/repos/a/b",
        "https://api.github.com/repos/a/b",
        403,
        {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(time.time() + 300)},
        b"",
    )
    delay = github_cooldown(response.status, response.headers)
    assert 290 < delay <= 300
    with pytest.raises(RequestError) as caught:
        require_success(response)
    assert caught.value.issue.retry_after_s > 290
    async with SafeFetcher(respect_robots=False) as fetcher:
        fetcher._github_until = time.monotonic() + delay

        async def forbidden():
            pytest.fail("A cooled-down host must not open a network session")

        monkeypatch.setattr(fetcher, "_get_session", forbidden)
        with pytest.raises(RequestError) as caught:
            await fetcher.get(response.url)
        assert caught.value.issue.code == "rate_limited"
        assert fetcher.budget.used == 0


def test_github_cooldown_header_edge_cases():
    from pwp.network import github_cooldown

    assert github_cooldown(200, {}) == 0
    assert github_cooldown(403, {}) == 0
    assert github_cooldown(429, {"Retry-After": "120"}) == 120
    assert github_cooldown(403, {"Retry-After": "120"}) == 120
    assert github_cooldown(403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "bad"}) == 60


@pytest.mark.parametrize(
    "value",
    [
        "socks5://127.0.0.1:7890",
        "http://proxy.example:80",
        "http://10.0.0.1:80",
        "http://user:pass@127.0.0.1:7890",
        "http://127.0.0.1:7890/path",
    ],
)
def test_invalid_proxy_configuration(value):
    with pytest.raises(ValueError):
        validate_proxy(value)


async def test_unavailable_proxy_has_no_direct_fallback():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    try:
        async with SafeFetcher(trusted_proxy=f"http://127.0.0.1:{port}", max_retries=0) as fetcher:
            with pytest.raises(RequestError) as caught:
                await fetcher.get("https://example.org/", check_robots=False)
            assert caught.value.issue.code == "fetch_failed"
    finally:
        sock.close()


async def test_explicit_proxy_and_private_redirect(monkeypatch):
    received = []

    async def proxy(reader, writer):
        request = await reader.readuntil(b"\r\n\r\n")
        received.append(request.decode())
        if b"/redirect " in request:
            response = (
                b"HTTP/1.1 302 Found\r\nLocation: http://169.254.169.254/\r\n"
                b"Content-Length: 0\r\nConnection: close\r\n\r\n"
            )
        else:
            response = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
        writer.write(response)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def forbidden(*args, **kwargs):
        pytest.fail("Target DNS must be delegated to the trusted proxy")

    monkeypatch.setattr(PublicResolver, "resolve", forbidden)
    server = await asyncio.start_server(proxy, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with (
        server,
        SafeFetcher(
            trusted_proxy=f"http://127.0.0.1:{port}", respect_robots=False, min_interval=0
        ) as fetcher,
    ):
        assert (await fetcher.get("http://project.example/")).body == b"ok"
        with pytest.raises(RequestError, match="Non-public"):
            await fetcher.get("http://project.example/redirect")
    assert len(received) == 2
    assert received[0].startswith("GET http://project.example/ HTTP/1.1")


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://10.0.0.1/",
        "http://169.254.169.254/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[2002:7f00:1::]/",
        "http://localhost/",
        "http://test.localhost/",
        "http://router.local/",
        "http://example.com:8000/",
        "file:///etc/passwd",
        "http://user:pass@example.com/",
        "https://arxiv.org/pdf/2410.12345",
        "https://arxiv.org/html/2410.12345",
        "https://example.com/paper.pdf",
    ],
)
def test_blocked_targets(url):
    with pytest.raises(RequestError):
        validate_target(url)


def test_public_addresses_and_normal_urls():
    assert public_address("8.8.8.8")
    assert not public_address("224.0.0.1")
    assert not public_address("192.168.1.1")
    assert not public_address("64:ff9b::7f00:1")
    assert validate_target("https://project.example/a#section") == "https://project.example/a"


async def test_connection_resolver_rejects_mixed_public_private_answers(monkeypatch):
    resolver = PublicResolver()

    async def answers(host, port, family):
        return [
            {
                "hostname": host,
                "host": "8.8.8.8",
                "port": port,
                "family": socket.AF_INET,
                "proto": 0,
                "flags": 0,
            },
            {
                "hostname": host,
                "host": "127.0.0.1",
                "port": port,
                "family": socket.AF_INET,
                "proto": 0,
                "flags": 0,
            },
        ]

    monkeypatch.setattr(resolver.delegate, "resolve", answers)
    try:
        with pytest.raises(OSError, match="non-public"):
            await resolver.resolve("rebind.example", 443)
    finally:
        await resolver.close()


async def test_actual_connector_uses_public_resolver():
    async with SafeFetcher() as fetcher:
        session = await fetcher._get_session()
        assert isinstance(session.connector._resolver, PublicResolver)
        assert not session.trust_env


async def test_redirect_validation_happens_before_connection(monkeypatch):
    calls = []

    async def request(url, headers, max_bytes):
        calls.append(url)
        return FetchResult(url, url, 302, {"location": "http://169.254.169.254/"}, b"")

    async with SafeFetcher(respect_robots=False) as fetcher:
        monkeypatch.setattr(fetcher, "_request", request)
        with pytest.raises(RequestError) as error:
            await fetcher.get("https://public.example/")
    assert error.value.issue.code == "blocked_url"
    assert calls == ["https://public.example/"]


async def test_cross_origin_redirect_strips_credentials(monkeypatch):
    calls = []

    async def request(url, headers, max_bytes):
        calls.append((url, headers.copy()))
        if len(calls) == 1:
            return FetchResult(url, url, 302, {"location": "https://second.example/"}, b"")
        return FetchResult(url, url, 200, {}, b"done")

    async with SafeFetcher(respect_robots=False) as fetcher:
        monkeypatch.setattr(fetcher, "_request", request)
        result = await fetcher.get(
            "https://first.example/", headers={"Authorization": "Bearer test"}
        )
    assert result.final_url == "https://second.example/"
    assert "Authorization" not in calls[1][1]
    assert result.history == ["https://first.example/"]


async def test_robots_denial_and_caching(monkeypatch):
    calls = []

    async def request(url, headers, max_bytes):
        calls.append(url)
        return FetchResult(url, url, 200, {}, b"User-agent: *\nDisallow: /private\n")

    async with SafeFetcher() as fetcher:
        monkeypatch.setattr(fetcher, "_request", request)
        for _ in range(2):
            with pytest.raises(RequestError) as error:
                await fetcher.get("https://project.example/private")
            assert error.value.issue.code == "robots_denied"
    assert calls == ["https://project.example/robots.txt"]


async def test_request_budget_and_scoped_budget_before_connect(monkeypatch):
    async with SafeFetcher(budget=RequestBudget(10), respect_robots=False) as fetcher:
        with request_scope(RequestBudget(0)):
            with pytest.raises(RequestError) as error:
                await fetcher.get("https://public.example/")
        assert error.value.issue.code == "request_limit"
        assert fetcher._session is None


async def test_https_downgrade_strips_authorization(monkeypatch):
    calls = []

    async def request(url, headers, max_bytes):
        calls.append(headers.copy())
        if len(calls) == 1:
            return FetchResult(url, url, 302, {"location": "http://public.example/"}, b"")
        return FetchResult(url, url, 200, {}, b"ok")

    async with SafeFetcher(respect_robots=False) as fetcher:
        monkeypatch.setattr(fetcher, "_request", request)
        await fetcher.get("https://public.example/", headers={"Authorization": "Bearer test"})
    assert "Authorization" not in calls[1]


async def test_network_body_limit_and_video_header_only(monkeypatch):
    class Content:
        reads = 0

        async def iter_chunked(self, size):
            self.reads += 1
            yield b"x" * 100

    class Response:
        status = 200
        url = "https://public.example/asset"
        headers = {"Content-Type": "video/mp4"}
        content = Content()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class Session:
        def get(self, *args, **kwargs):
            return response

    response = Response()

    async def session():
        return Session()

    async with SafeFetcher(respect_robots=False, min_interval=0, max_retries=0) as fetcher:
        monkeypatch.setattr(fetcher, "_get_session", session)
        result = await fetcher.get("https://public.example/asset", omit_video=True)
        assert result.body == b"" and response.content.reads == 0
        response.headers = {"Content-Type": "image/png"}
        with pytest.raises(RequestError) as error:
            await fetcher.get("https://public.example/asset", max_bytes=10)
        assert error.value.issue.code == "response_too_large"
        response.headers = {"Content-Type": "application/pdf"}
        with pytest.raises(RequestError) as error:
            await fetcher.get("https://public.example/asset")
        assert error.value.issue.code == "document_blocked"


async def test_concurrent_robots_checks_share_one_request():
    import asyncio

    from pwp.network import FetchResult, SafeFetcher

    calls = []
    async with SafeFetcher(max_connections=16) as fetcher:

        async def response(url, **kwargs):
            calls.append(url)
            await asyncio.sleep(0.01)
            return FetchResult(url, url, 200, {}, b"User-agent: *\nAllow: /\n")

        fetcher.get = response
        await asyncio.gather(
            *(fetcher._check_robots(f"https://example.org/page-{i}") for i in range(8))
        )
        assert calls == ["https://example.org/robots.txt"]
        session = await fetcher._get_session()
        assert session.connector.limit == 16
        assert session.connector.limit_per_host == 4


@pytest.mark.parametrize("maximum", [0, 33, 1.5])
def test_invalid_connection_limit(maximum):
    from pwp.network import SafeFetcher

    with pytest.raises(ValueError):
        SafeFetcher(max_connections=maximum)


async def test_robots_unavailable_cools_down_and_then_recovers(monkeypatch):
    calls = []
    clock = [100.0]
    monkeypatch.setattr("pwp.network.time.monotonic", lambda: clock[0])
    async with SafeFetcher() as fetcher:

        async def response(url, **kwargs):
            calls.append(url)
            return FetchResult(url, url, 503 if len(calls) == 1 else 404, {}, b"")

        monkeypatch.setattr(fetcher, "get", response)
        for _ in range(3):
            with pytest.raises(RequestError) as error:
                await fetcher._check_robots("https://example.org/image.png")
            assert error.value.issue.code == "robots_unavailable"
            assert error.value.issue.retry_after_s > 0
        assert len(calls) == 1
        clock[0] += 31
        await fetcher._check_robots("https://example.org/image.png")
        assert len(calls) == 2


async def test_github_uses_its_own_bounded_session():
    async with SafeFetcher(max_connections=8) as fetcher:
        web = await fetcher._get_session()
        github = await fetcher._get_session(github=True)
        assert web is not github
        assert web.connector.limit == 8
        assert github.connector.limit == github.connector.limit_per_host == 1
        assert isinstance(github.connector._resolver, PublicResolver)
        assert not github.trust_env
    assert web.closed and github.closed


async def test_github_quota_diagnostics_do_not_retain_credentials(monkeypatch):
    import time

    class Response:
        status = 200
        url = "https://api.github.com/repos/example/project"
        headers = {
            "X-RateLimit-Limit": "5000",
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": str(int(time.time()) + 120),
        }

        @property
        def content(self):
            return self

        async def iter_chunked(self, size):
            yield b"{}"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class Session:
        def get(self, *args, **kwargs):
            return Response()

    async def session(**kwargs):
        return Session()

    async with SafeFetcher(respect_robots=False) as fetcher:
        monkeypatch.setattr(fetcher, "_get_session", session)
        await fetcher.get(Response.url, headers={"Authorization": "Bearer private-token"})
        stats = fetcher.statistics()["github"]
        assert stats["quota"]["remaining"] == 0
        assert stats["quota"]["limit"] == 5000
        assert 0 < stats["cooldown_remaining_s"] <= 120
        assert "private-token" not in str(stats)
        stats["quota"]["remaining"] = -1
        assert fetcher.statistics()["github"]["quota"]["remaining"] == 0


async def test_host_queue_cancellation_and_independent_network_metrics(monkeypatch):
    import asyncio

    entered = asyncio.Event()
    release = asyncio.Event()
    tasks = []
    async with SafeFetcher(respect_robots=False, min_interval=0) as fetcher:

        async def read(session, url, headers, max_bytes, omit_video):
            if "api.github.com" in url:
                entered.set()
                await release.wait()
            return FetchResult(url, url, 200, {}, b"ok")

        monkeypatch.setattr(fetcher, "_read_response", read)
        try:
            tasks.append(asyncio.create_task(fetcher.get("https://api.github.com/repos/a/b")))
            await asyncio.wait_for(entered.wait(), 1)
            tasks.append(asyncio.create_task(fetcher.get("https://api.github.com/repos/c/d")))
            await asyncio.sleep(0)
            assert fetcher.statistics()["github"]["host_queue_active"] == 1
            # A stalled repository does not occupy web admission or its connector.
            await asyncio.wait_for(fetcher.get("https://project.example/"), 1)
            tasks[1].cancel()
            await asyncio.gather(tasks[1], return_exceptions=True)
            release.set()
            await tasks[0]
            stats = fetcher.statistics()
            assert stats["github"]["completed"] == stats["github"]["cancelled"] == 1
            assert stats["github"]["host_queue_s"] > 0
            assert stats["web"]["completed"] == 1 and stats["web"]["bytes"] == 2
            for lane in stats.values():
                assert lane["active"] == lane["host_queue_active"] == lane["network_active"] == 0
            stats["github"]["completed"] = -1
            assert fetcher.statistics()["github"]["completed"] == 1
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
