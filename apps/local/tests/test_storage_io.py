"""Exercise Windows-style sharing conflicts and threaded gallery readers."""

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from app.server import create_app
from app.storage import atomic_json, read_json
from fastapi.testclient import TestClient


def test_transient_read_and_replace_conflicts_are_retried(tmp_path, monkeypatch):
    path = tmp_path / "latest.json"
    atomic_json(path, {"revision": 1})
    original_read, original_replace = Path.read_text, os.replace
    reads = writes = 0
    monkeypatch.setattr("app.storage.time.sleep", lambda delay: None)

    def read(self, *args, **kwargs):
        nonlocal reads
        reads += 1
        if reads < 3:
            raise PermissionError("sharing violation")
        return original_read(self, *args, **kwargs)

    def replace(source, target):
        nonlocal writes
        writes += 1
        if writes < 3:
            raise PermissionError("sharing violation")
        return original_replace(source, target)

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(os, "replace", replace)
    assert read_json(path) == {"revision": 1}
    atomic_json(path, {"revision": 2})
    assert read_json(path) == {"revision": 2}
    assert reads == 4 and writes == 3
    assert list(tmp_path.glob("*.tmp")) == []


def test_permanent_permission_error_is_not_treated_as_missing(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError("persistent access denial")

    monkeypatch.setattr("app.storage.time.sleep", lambda delay: None)
    monkeypatch.setattr(Path, "read_text", denied)
    with pytest.raises(PermissionError):
        read_json(tmp_path / "latest.json")


def test_concurrent_pointer_reads_observe_complete_json(tmp_path):
    path = tmp_path / "latest.json"
    atomic_json(path, {"revision": 0})

    def write():
        for revision in range(1, 51):
            atomic_json(path, {"revision": revision})

    def read():
        for _ in range(200):
            assert isinstance(read_json(path)["revision"], int)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(write), *(pool.submit(read) for _ in range(3))]
        for future in futures:
            future.result()
    assert read_json(path) == {"revision": 50}


def test_gallery_returns_retryable_response_for_persistent_lock(tmp_path, monkeypatch):
    def denied(self):
        raise PermissionError("sharing violation")

    monkeypatch.setattr("app.storage.Store.gallery", denied)
    with TestClient(create_app(tmp_path)) as client:
        response = client.get("/api/gallery")
        assert response.status_code == 503
        assert response.headers["Retry-After"] == "2"
