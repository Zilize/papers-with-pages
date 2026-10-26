import asyncio
from datetime import UTC, date, datetime, timedelta
from email.utils import format_datetime
from types import SimpleNamespace

import httpx
import pytest
from pwp import ArxivClient, MetadataError, PaperNotFoundError, PwpError
from pwp.harvest import http as metadata_http
from pwp.harvest.http import retry_after_seconds


@pytest.fixture
def clock(monkeypatch):
    state = SimpleNamespace(now=0.0, sleeps=[])
    original_sleep = asyncio.sleep

    async def sleep(seconds):
        state.sleeps.append(seconds)
        state.now += seconds
        await original_sleep(0)

    monkeypatch.setattr(metadata_http, "time", SimpleNamespace(monotonic=lambda: state.now))
    monkeypatch.setattr(metadata_http, "asyncio", SimpleNamespace(Lock=asyncio.Lock, sleep=sleep))
    return state


async def test_lookup_order_versions_missing_and_borrowed_client(atom_xml):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=atom_xml)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        async with ArxivClient(client=http, min_interval=0) as client:
            results = await client.get_papers(
                [
                    "hep-th/9901001",
                    "2410.12345v2",
                    "2410.99999",
                    "2410.12345v1",
                    "2410.12345v2",
                ]
            )
        assert not http.is_closed
    assert results[0].paper.arxiv_id == "hep-th/9901001"
    assert results[1].paper.version == 2
    assert results[2].issue.code == "not_found"
    assert results[3].issue.code == "not_found"
    assert results[4] == results[1]
    assert requests[0].url.params["max_results"] == "4"


async def test_batch_failure_is_retained_without_hiding_later_results(atom_xml):
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        return httpx.Response(403) if count == 1 else httpx.Response(200, content=atom_xml)

    events = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        async with ArxivClient(client=http, min_interval=0) as client:
            results = await client.get_papers(
                ["2410.12345", "hep-th/9901001"], batch_size=1, on_event=events.append
            )
    assert results[0].issue.code == "http_error"
    assert results[1].paper is not None
    assert events[0].type == "step_started"
    assert events[-1].completed == 2
    assert events[-1].type == "step_completed"
    assert events[-1].issue.code == "lookup_failures"
    assert len({event.operation_id for event in events}) == 1


async def test_all_failed_lookup_has_one_failed_terminal_event():
    events = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(403))
    ) as http:
        async with ArxivClient(client=http, min_interval=0) as client:
            result = await client.get_papers(["2410.12345"], on_event=events.append)
    assert result[0].issue is not None
    assert events[-1].type == "step_failed"
    assert sum(event.type in {"step_completed", "step_failed"} for event in events) == 1


async def test_single_lookup_errors(atom_xml):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=atom_xml))
    ) as http:
        async with ArxivClient(client=http, min_interval=0) as client:
            with pytest.raises(PaperNotFoundError):
                await client.get_paper("2410.99999")
            with pytest.raises(ValueError):
                await client.get_papers(["2410.12345", "invalid"])
            with pytest.raises(TypeError):
                await client.get_papers("2410.12345")
            assert await client.get_papers([]) == []


async def test_oai_pagination_and_token_only_requests(oai_xml):
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, content=oai_xml)
        return httpx.Response(
            200,
            content=b"""<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
          <responseDate>2026-10-10T04:00:05Z</responseDate>
          <ListRecords><resumptionToken/></ListRecords></OAI-PMH>""",
        )

    events = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        async with ArxivClient(client=http, min_interval=0) as client:
            batches = [
                batch
                async for batch in client.harvest(
                    set_spec="cs:cs:CV",
                    metadata_updated_from=date(2026, 10, 1),
                    on_event=events.append,
                )
            ]
    assert len(batches) == 2
    assert requests[0].url.params["from"] == "2026-10-01"
    assert dict(requests[1].url.params) == {
        "verb": "ListRecords",
        "resumptionToken": "next+token/=",
    }
    assert events[-1].type == "step_completed"
    assert events[-1].completed == 3
    assert all(event.total is None for event in events)


async def test_oai_resume_validation_and_loop_detection(oai_xml):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=oai_xml)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        async with ArxivClient(client=http, min_interval=0) as client:
            with pytest.raises(ValueError):
                await anext(client.harvest(resumption_token="token", set_spec="cs"))
            with pytest.raises(ValueError):
                await anext(
                    client.harvest(
                        metadata_updated_from=date(2026, 10, 2),
                        metadata_updated_until=date(2026, 10, 1),
                    )
                )
            assert requests == []
            with pytest.raises(MetadataError) as error:
                async for _ in client.harvest(resumption_token="previous-token"):
                    pass
    assert error.value.issue.code == "repeated_token"
    assert dict(requests[0].url.params) == {
        "verb": "ListRecords",
        "resumptionToken": "previous-token",
    }
    assert len(requests) == 2


async def test_retry_after_and_default_spacing(atom_xml, clock):
    times = []

    def handler(request):
        times.append(clock.now)
        if len(times) == 1:
            return httpx.Response(503, headers={"Retry-After": "7"})
        return httpx.Response(200, content=atom_xml)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        async with ArxivClient(client=http) as client:
            await client.get_paper("2410.12345")
            await client.get_paper("2410.12345")
    assert times == [0, 7, 10]


async def test_long_retry_after_returns_without_waiting_or_bypassing_cooldown(clock):
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        return httpx.Response(429, headers={"Retry-After": "3600"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        async with ArxivClient(client=http, max_retry_wait=60) as client:
            with pytest.raises(PwpError) as error:
                await client.get_paper("2410.12345")
            assert error.value.issue.retry_after_s == 3600
            with pytest.raises(PwpError) as repeated:
                await client.get_paper("2410.12345")
    assert repeated.value.issue.retryable
    assert count == 1
    assert clock.now == 0


async def test_transport_failure_retries_are_bounded(clock):
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        raise httpx.ReadTimeout("Synthetic timeout", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        async with ArxivClient(client=http, min_interval=0, max_retries=2) as client:
            result = (await client.get_papers(["2410.12345"]))[0]
    assert count == 3
    assert result.issue.code == "transport_error"
    assert result.issue.retryable


async def test_callbacks_propagate_and_cancellation_is_not_a_result(atom_xml):
    def fail(event):
        raise RuntimeError("Broken event consumer")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=atom_xml))
    ) as http:
        async with ArxivClient(client=http, min_interval=0) as client:
            with pytest.raises(RuntimeError, match="Broken event consumer"):
                await client.get_paper("2410.12345", on_event=fail)

    entered = asyncio.Event()
    events = []

    async def blocked(request):
        entered.set()
        await asyncio.Event().wait()

    async with httpx.AsyncClient(transport=httpx.MockTransport(blocked)) as http:
        async with ArxivClient(client=http, min_interval=0) as client:
            task = asyncio.create_task(client.get_paper("2410.12345", on_event=events.append))
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert all(event.type != "step_completed" for event in events)


async def test_concurrent_calls_share_request_gate(atom_xml, clock):
    active = 0
    maximum = 0
    times = []

    async def handler(request):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        times.append(clock.now)
        await asyncio.sleep(0)
        active -= 1
        return httpx.Response(200, content=atom_xml)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        async with ArxivClient(client=http) as client:
            await asyncio.gather(*(client.get_paper("2410.12345") for _ in range(3)))
    assert maximum == 1
    assert times == [0, 3, 6]


async def test_response_limit_and_redirect_rejection(atom_xml):
    responses = [
        httpx.Response(200, content=atom_xml),
        httpx.Response(302, headers={"Location": "http://127.0.0.1/private"}),
    ]
    calls = []

    def handler(request):
        calls.append(request)
        return responses.pop(0)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as http:
        async with ArxivClient(client=http, min_interval=0, max_response_bytes=10) as client:
            first = (await client.get_papers(["2410.12345"]))[0]
            second = (await client.get_papers(["2410.12345"]))[0]
    assert first.issue.code == "response_too_large"
    assert second.issue.code == "http_error"
    assert len(calls) == 2


async def test_early_harvest_close_does_not_report_completion(oai_xml):
    events = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=oai_xml))
    ) as http:
        async with ArxivClient(client=http, min_interval=0) as client:
            stream = client.harvest(on_event=events.append)
            await anext(stream)
            await stream.aclose()
    assert all(event.type != "step_completed" for event in events)


def test_retry_after_parser():
    assert retry_after_seconds("12") == 12
    assert retry_after_seconds("-1") == 0
    assert retry_after_seconds("NaN") is None
    assert retry_after_seconds("invalid") is None
    future = format_datetime(datetime.now(UTC) + timedelta(seconds=30), usegmt=True)
    assert 28 <= retry_after_seconds(future) <= 30
