"""Exercise persistence and lifecycle rules with no upstream network access."""

import asyncio
import shutil
from datetime import date

import pytest
from app.jobs import Collector, HarvestRequest
from app.server import create_app
from app.storage import Store, paper_key, read_json
from fastapi.testclient import TestClient
from filelock import FileLock, Timeout
from pwp.models import (
    HarvestBatch,
    HarvestRecord,
    Issue,
    Paper,
    PaperResult,
    ResourceLink,
    StepOutcome,
)


def paper(**values):
    values.setdefault("comments", "https://example.org/project https://github.com/example/research")
    return Paper(arxiv_id="2601.12345", title="A useful research project", **values)


def record(p=None, stamp="2026-01-03"):
    p = p or paper()
    return HarvestRecord(
        identifier=f"oai:arXiv.org:{p.arxiv_id}", paper=p, datestamp=date.fromisoformat(stamp)
    )


def result(p=None, status="completed", resources=True):
    p = p or paper()
    return PaperResult(
        requested=p.identifier,
        paper=p,
        status=status,
        resources=[ResourceLink(url="https://example.org/project", kind="project_page")]
        if resources
        else [],
    )


class Client:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def harvest(self, **kwargs):
        yield HarvestBatch(records=[record(), record()])


async def resolver(paper, **kwargs):
    return result(paper)


async def test_host_rate_limit_does_not_reduce_global_admission(tmp_path, monkeypatch):
    reports = []

    async def limited(paper, **kwargs):
        output = result(paper, status="partial")
        steps = [
            StepOutcome(
                step="inspect_repo",
                status="failed",
                issue=Issue(code="rate_limited", message="Quota exhausted", retryable=True),
            )
        ]
        return output.model_copy(update={"steps": steps})

    async def capture_admission(items, process, **kwargs):
        for item in items:
            reports.append(await process(item))

    monkeypatch.setattr("app.jobs.adaptive_map", capture_admission)
    collector = Collector(Store(tmp_path), Client, limited)
    collector.start(request())
    await collector.task
    assert reports == [False]
    assert collector.state["status"] == "completed"
    assert collector.state["failures"][0]["message"] == "Quota exhausted"
    assert set(collector.state["network"]) == {"github", "web"}


def request(**kwargs):
    return HarvestRequest(start=date(2026, 1, 1), end=date(2026, 1, 3), **kwargs)


def test_dedup_no_link_reuse_and_metadata_revision(tmp_path):
    store = Store(tmp_path)
    key = store.merge(record())
    store.merge(record())
    store.save_result(key, "one", result(resources=False))
    assert store.gallery() == {"items": [], "saved": 1}
    assert store.reusable(key)
    store.merge(record(stamp="2026-01-04"))
    assert store.reusable(key)
    store.merge(record(paper(abstract="Updated abstract"), "2026-01-05"))
    assert not store.reusable(key)
    store.merge(record(stamp="2026-01-02"))
    assert store.metadata(key)["paper"]["abstract"] == "Updated abstract"


@pytest.mark.parametrize("status", ["partial", "failed"])
def test_failed_attempt_keeps_previous_card_and_is_retried(tmp_path, status):
    store = Store(tmp_path)
    key = store.merge(record())
    store.save_result(key, "one", result())
    store.save_result(key, "two", result(status=status, resources=False))
    assert store.current(key)["run_id"] == "one"
    assert store.detail(key)["stale"]
    assert not store.reusable(key)
    assert len(store.gallery()["items"]) == 1
    store.save_result(key, "three", result())
    assert store.reusable(key)
    assert not store.detail(key)["stale"]


def test_deleted_and_old_identifiers(tmp_path):
    store = Store(tmp_path)
    old = Paper(
        arxiv_id="hep-th/9901001", title="Legacy research", comments="https://example.org/project"
    )
    key = store.merge(record(old))
    store.save_result(key, "one", result(old))
    assert "/" not in key
    assert len(store.gallery()["items"]) == 1
    store.merge(
        HarvestRecord(
            identifier="oai:arXiv.org:hep-th/9901001", datestamp=date(2026, 1, 5), status="deleted"
        )
    )
    assert store.detail(key) is None

    assert store.gallery()["items"] == []
    store.merge(record(old))
    assert store.detail(key) is None


def test_saved_publication_link_is_excluded_from_gallery_without_rewriting(tmp_path):
    store = Store(tmp_path)
    key = store.merge(record())
    old = result().model_copy(
        update={
            "resources": [
                ResourceLink(url="https://openreview.net/forum?id=test", kind="project_page"),
                ResourceLink(url="https://github.com/example/research", kind="repository"),
            ]
        }
    )
    store.save_result(key, "old", old)
    gallery = store.gallery()
    assert [r["kind"] for r in gallery["items"][0]["resources"]] == ["repository"]
    saved = read_json(tmp_path / "papers" / key / "results" / "old.json")
    assert saved["resources"][0]["kind"] == "project_page"


async def test_collection_deduplicates_reuses_and_persists_raw_batches(tmp_path):
    calls = []

    async def resolve(p, **kwargs):
        calls.append(p.arxiv_id)
        return result(p)

    store = Store(tmp_path)
    collector = Collector(store, Client, resolve)
    collector.start(request())
    with pytest.raises(RuntimeError, match="already running"):
        collector.start(request())
    await collector.task
    assert collector.state["harvested"] == 2
    assert collector.state["total"] == collector.state["completed"] == 1
    assert len(calls) == 1
    assert list((tmp_path / "runs" / collector.state["id"]).glob("batch-*.json"))
    collector.start(request())
    await collector.task
    assert collector.state["reused"] == 1
    assert len(calls) == 1
    collector.start(request(refresh=True))
    await collector.task
    assert len(calls) == 2


async def test_unexpected_processing_failure_retains_result(tmp_path):
    store = Store(tmp_path)
    key = store.merge(record())
    store.save_result(key, "old", result())

    async def fail(*args, **kwargs):
        raise RuntimeError("Browser unavailable")

    collector = Collector(store, Client, fail)
    collector.start(request(refresh=True))
    await collector.task
    assert collector.state["failures"][0]["message"] == "Browser unavailable"
    assert store.detail(key)["stale"]
    assert not store.reusable(key)


async def test_shutdown_and_recovery(tmp_path):
    entered = asyncio.Event()

    async def slow(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    store = Store(tmp_path)
    collector = Collector(store, Client, slow)
    collector.start(request())
    await entered.wait()
    await collector.close()
    assert store.latest_run()["status"] == "interrupted"
    state = collector.state | {"status": "running"}
    store.save_run(state)
    store.recover_runs()
    assert Store(tmp_path).latest_run()["status"] == "interrupted"


async def test_shutdown_before_collection_task_enters(tmp_path):
    collector = Collector(Store(tmp_path), Client, resolver)
    collector.start(request())
    await collector.close()
    assert collector.store.latest_run()["status"] == "interrupted"


async def test_collection_has_two_workers_and_shared_sessions(tmp_path, monkeypatch):
    from app.adaptive import GIB, Load

    monkeypatch.setattr("app.adaptive.sample_load", lambda: Load(20, 8 * GIB, 30))
    monkeypatch.setattr("app.adaptive.os.cpu_count", lambda: 8)
    entered = asyncio.Event()
    release = asyncio.Event()
    sessions = []
    active = 0
    peak = 0

    class BatchClient(Client):
        async def harvest(self, **kwargs):
            yield HarvestBatch(
                records=[
                    record(Paper(arxiv_id=f"2601.1000{i}", title=f"Paper {i}")) for i in range(4)
                ]
            )

    async def work(p, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        sessions.append((kwargs["fetcher"], kwargs["render_session"]))
        if active == 2:
            entered.set()
        try:
            await release.wait()
            return result(p)
        finally:
            active -= 1

    collector = Collector(Store(tmp_path), BatchClient, work)
    collector.start(request())
    await asyncio.wait_for(entered.wait(), 5)
    assert len(sessions) == 2
    release.set()
    await collector.task
    assert peak == 2 and collector.state["completed"] == 4
    assert all(pair == sessions[0] for pair in sessions)
    assert sessions[0][1]._closed
    assert sessions[0][1]._browser is None


async def test_invalid_deleted_records_retained_without_resolve(tmp_path):
    class SpecialClient(Client):
        async def harvest(self, **kwargs):
            yield HarvestBatch(
                records=[
                    HarvestRecord(
                        identifier="bad",
                        status="invalid",
                        issue=Issue(code="bad", message="Malformed record"),
                    ),
                    HarvestRecord(identifier="oai:arXiv.org:2601.12345", status="deleted"),
                ]
            )

    async def unexpected(*args, **kwargs):
        pytest.fail("No active records should be processed")

    collector = Collector(Store(tmp_path), SpecialClient, unexpected)
    collector.start(request())
    await collector.task
    assert collector.state["invalid"] == collector.state["deleted"] == 1
    assert collector.state["total"] == 0
    batch = read_json(next((tmp_path / "runs" / collector.state["id"]).glob("batch-*.json")))
    assert len(batch["records"]) == 2


def test_api_validation_boundaries_and_static_files(tmp_path):
    with TestClient(
        create_app(tmp_path, client_factory=Client, resolver=resolver, github_token="fixture_token")
    ) as client:
        assert client.get("/").status_code == 200
        assert client.get("/static/app.js").status_code == 200
        assert client.get("/api/gallery").json() == {"items": [], "saved": 0}
        assert client.get("/api/papers/not-a-key").status_code == 404
        body = {"start": "2026-01-01", "end": "2026-01-03"}
        assert client.post("/api/collection", json=body).status_code == 403
        headers = {"X-PWP-App": "1", "Origin": "https://untrusted.example"}
        assert client.post("/api/collection", json=body, headers=headers).status_code == 403
        headers = {"X-PWP-App": "1"}
        invalid = body | {"start": "2026-01-05"}
        assert client.post("/api/collection", json=invalid, headers=headers).status_code == 422
        assert client.post("/api/collection", json=body, headers=headers).status_code == 202
        assert client.get("/assets/../papers/metadata.json").status_code == 404
        assert client.get("/", headers={"Host": "untrusted.example"}).status_code == 400


def test_stop_endpoint_is_protected_and_allows_restart(tmp_path):
    class WaitingClient(Client):
        async def harvest(self, **kwargs):
            await asyncio.Event().wait()
            yield  # pragma: no cover

    with TestClient(
        create_app(tmp_path, client_factory=WaitingClient, github_token="fixture_token")
    ) as client:
        headers = {"X-PWP-App": "1"}
        body = {"start": "2026-01-01", "end": "2026-01-03"}
        assert client.post("/api/collection/stop").status_code == 403
        assert client.post("/api/collection/stop", headers=headers).json() is None
        for _ in range(2):
            assert client.post("/api/collection", json=body, headers=headers).status_code == 202
            response = client.post("/api/collection/stop", headers=headers)
            assert response.status_code == 200
            assert response.json()["status"] == "interrupted"
            assert client.post("/api/collection/stop", headers=headers).json() == response.json()
            assert Store(tmp_path).latest_run()["status"] == "interrupted"


async def test_repeated_stop_preserves_completed_results(tmp_path):
    entered = asyncio.Event()
    cleaned = 0

    async def slow(*args, **kwargs):
        nonlocal cleaned
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0.01)
            cleaned += 1

    store = Store(tmp_path)
    collector = Collector(store, Client, resolver)
    collector.start(request())
    await collector.task
    saved = store.gallery()
    collector.resolver = slow
    collector.start(request(refresh=True))
    await entered.wait()
    await asyncio.gather(collector.close(), collector.close())
    assert cleaned > 0
    assert store.gallery() == saved
    assert collector.state["status"] == "interrupted"


def test_deleted_collection_recovers_without_server_restart(tmp_path):
    store = Store(tmp_path)
    store.merge(record())
    store.save_run({"id": "previous", "status": "completed"})
    with TestClient(create_app(tmp_path, client_factory=Client, resolver=resolver)) as client:
        assert client.get("/api/gallery").json()["saved"] == 1

        assert client.get("/api/collection").json()["id"] == "previous"
        for name in ("papers", "assets", "runs"):
            shutil.rmtree(tmp_path / name)
        assert client.get("/api/gallery").json() == {"items": [], "saved": 0}
        assert client.get("/api/collection").json() is None
        assert all((tmp_path / name).is_dir() for name in ("papers", "assets", "runs"))
        store.merge(record())
        assert client.get("/api/gallery").json()["saved"] == 1


@pytest.mark.parametrize(
    "code,reuse", [("asset_timeout", False), ("rate_limited", False), ("robots_denied", True)]
)
def test_completed_results_with_transient_warnings_are_retried(tmp_path, code, reuse):
    store = Store(tmp_path)
    key = store.merge(record())
    output = result()
    output.steps.extend(
        [StepOutcome(step="render_page", status="completed", issue=Issue(code=code, message=code))]
    )
    store.save_result(key, "fixture", output)
    assert store.reusable(key) is reuse


def test_data_directory_lock(tmp_path):
    with FileLock(tmp_path / ".app.lock"), pytest.raises(Timeout):
        with TestClient(create_app(tmp_path)):
            pass


async def test_paper_deadline_releases_worker_and_continues(tmp_path, monkeypatch):
    monkeypatch.setattr("app.jobs.PAPER_TIMEOUT_SECONDS", 0.05)
    visited = []

    class TwoPapers(Client):
        async def harvest(self, **kwargs):
            yield HarvestBatch(
                records=[record(Paper(arxiv_id=f"2601.1000{i}", title=str(i))) for i in range(2)]
            )

    async def work(p, **kwargs):
        visited.append(p.arxiv_id)
        if p.arxiv_id.endswith("0"):
            await asyncio.Event().wait()
        return result(p)

    collector = Collector(Store(tmp_path), TwoPapers, work, max_workers=1)
    collector.start(request())
    await asyncio.wait_for(collector.task, 5)
    assert len(visited) == 2
    assert collector.state["completed"] == 2
    assert len(collector.state["failures"]) == 1
    assert "exceeded" in collector.state["failures"][0]["message"]
    assert collector.state["workers"] == []
    failed = read_json(
        tmp_path / "papers" / paper_key(visited[0]) / "results" / f"{collector.state['id']}.json"
    )
    assert failed["steps"][0]["issue"]["code"] == "paper_timeout"
    assert failed["steps"][0]["issue"]["retryable"]
    assert not collector.store.reusable(paper_key(visited[0]))


def test_gallery_read_does_not_block_status_and_coalesces_requests(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    entered, release = threading.Event(), threading.Event()
    original = Store.gallery
    calls = []

    def slow(store):
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return original(store)

    monkeypatch.setattr(Store, "gallery", slow)
    with TestClient(create_app(tmp_path)) as client, ThreadPoolExecutor(3) as pool:
        first = pool.submit(client.get, "/api/gallery")
        assert entered.wait(5)
        second = pool.submit(client.get, "/api/gallery")
        try:
            status = pool.submit(client.get, "/api/collection")
            assert status.result(timeout=2).status_code == 200
        finally:
            release.set()
        assert first.result(timeout=5).json() == second.result(timeout=5).json()
    assert len(calls) == 1


def test_storage_is_portable_and_rejects_assets_outside_root(tmp_path):
    from datetime import UTC, datetime

    from pwp.models import MediaArtifact

    store = Store(tmp_path)
    key = store.merge(record())
    artifact = MediaArtifact(
        id="cover",
        kind="hero_screenshot",
        path=tmp_path / "assets" / "cover.png",
        source_url="https://example.org",
        content_type="image/png",
        bytes=1,
        sha256="abc",
        width=1,
        height=1,
        captured_at=datetime.now(UTC),
    )
    store.save_result(key, "portable", result().model_copy(update={"media": [artifact]}))
    assert store.detail(key)["result"]["media"][0]["path"] == "cover.png"
    outside = artifact.model_copy(update={"path": tmp_path.parent / "secret.png"})
    with pytest.raises(ValueError):
        store.save_result(key, "outside", result().model_copy(update={"media": [outside]}))
    assert store.current(paper_key("2601.12345"))["run_id"] == "portable"


@pytest.mark.parametrize("pages,workers,expected", [(6, 8, 6), (6, 2, 2), (3, 8, 3)])
async def test_configurable_render_page_limit(tmp_path, monkeypatch, pages, workers, expected):
    monkeypatch.setattr("app.adaptive.os.cpu_count", lambda: 16)
    seen = []

    async def work(p, **kwargs):
        seen.append(kwargs["render_session"].concurrency)
        return result(p)

    collector = Collector(Store(tmp_path), Client, work, max_workers=workers, render_pages=pages)
    collector.start(request())
    await collector.task
    assert collector.state["status"] == "completed"
    assert seen == [expected]
    assert collector.state["concurrency"]["render_limit"] == expected


@pytest.mark.parametrize("pages", [0, 17, 1.5, True])
def test_invalid_render_page_limit(tmp_path, pages):
    with pytest.raises(ValueError, match="Render pages"):
        Collector(Store(tmp_path), render_pages=pages)


def test_gallery_index_reuses_unchanged_papers_and_refreshes_changed_ones(tmp_path, monkeypatch):
    store = Store(tmp_path)
    key = store.merge(record())
    store.save_result(key, "first", result())
    assert len(store.gallery()["items"]) == 1
    assert read_json(tmp_path / "gallery-index.json")["entries"][key]["item"]

    calls = []
    original = Store.gallery_entry
    monkeypatch.setattr(
        Store, "gallery_entry", lambda self, k: calls.append(k) or original(self, k)
    )
    assert len(store.gallery()["items"]) == 1
    assert calls == []

    store.save_result(key, "second", result(resources=False))
    assert store.gallery() == {"items": [], "saved": 1}
    assert calls == [key]


def test_thumbnail_is_generated_cached_and_confined_to_assets(tmp_path):
    import io

    from PIL import Image

    source = tmp_path / "assets" / "paper" / "run" / "hero.png"
    source.parent.mkdir(parents=True)
    Image.new("RGB", (1440, 900), "white").save(source)
    (tmp_path / "secret.png").write_bytes(source.read_bytes())
    with TestClient(create_app(tmp_path)) as client:
        for query, size in (("", (480, 300)), ("?w=480", (480, 300)), ("?w=960", (960, 600))):
            response = client.get(f"/thumbnails/paper/run/hero.png{query}")
            assert response.status_code == 200
            assert "immutable" in response.headers["Cache-Control"]
            with Image.open(io.BytesIO(response.content)) as image:
                assert (image.format, image.size) == ("WEBP", size)
        assert (source.parent / "hero.w480.webp").is_file()
        assert (source.parent / "hero.w960.webp").is_file()
        assert client.get("/thumbnails/paper/run/hero.png?w=640").status_code == 404
        assert client.get("/thumbnails/../secret.png").status_code == 404
        assert client.get("/thumbnails/paper/run/missing.png").status_code == 404
