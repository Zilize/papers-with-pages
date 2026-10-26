"""Tokens persist only in the OS credential service, never collection data."""

from app import __main__ as entry
from app.server import create_app
from fastapi.testclient import TestClient


def test_cli_passes_environment_token_without_starting_server(monkeypatch):
    received = {}
    monkeypatch.setenv("PWP_GITHUB_TOKEN", "test-public-token")
    monkeypatch.setenv("GITHUB_TOKEN", "fallback-token")
    monkeypatch.setattr("sys.argv", ["pwp-app"])

    def factory(*args, **kwargs):
        received.update(kwargs)
        return object()

    monkeypatch.setattr(entry, "create_app", factory)
    monkeypatch.setattr(entry.uvicorn, "run", lambda *args, **kwargs: None)
    entry.main()
    assert received["github_token"] == "test-public-token"
    monkeypatch.delenv("PWP_GITHUB_TOKEN")
    entry.main()
    assert received["github_token"] == "fallback-token"


def test_ui_token_is_private_persistent_and_clearable(tmp_path):
    app = create_app(tmp_path)
    secret = "github_pat_test_secret"
    with TestClient(app) as client:
        assert not client.get("/api/settings/github").json()["configured"]
        blocked = client.post(
            "/api/collection",
            json={"start": "2026-01-01", "end": "2026-01-03"},
            headers={"X-PWP-App": "1"},
        )
        assert blocked.status_code == 400
        assert "GitHub token" in blocked.json()["detail"]
        assert app.state.collector.task is None
        assert client.put("/api/settings/github", json={"token": secret}).status_code == 403
        response = client.put(
            "/api/settings/github", json={"token": secret}, headers={"X-PWP-App": "1"}
        )
        assert response.json()["configured"] and response.json()["persisted"]
        assert app.state.collector.github_token == secret
        assert secret not in client.get("/api/settings/github").text
        assert secret not in client.get("/api/collection").text
        bad = client.put(
            "/api/settings/github", json={"token": secret + "\n"}, headers={"X-PWP-App": "1"}
        )
        assert bad.status_code == 400 and secret not in bad.text
        cleared = client.put(
            "/api/settings/github", json={"token": ""}, headers={"X-PWP-App": "1"}
        ).json()
        assert not cleared["configured"] and not cleared["persisted"]
        client.put("/api/settings/github", json={"token": secret}, headers={"X-PWP-App": "1"})
    with TestClient(create_app(tmp_path)) as restarted:
        state = restarted.get("/api/settings/github").json()
        assert state["configured"] and state["persisted"]
        restarted.put("/api/settings/github", json={"token": ""}, headers={"X-PWP-App": "1"})
    with TestClient(create_app(tmp_path)) as cleared:
        assert not cleared.get("/api/settings/github").json()["configured"]
    for path in tmp_path.rglob("*.json"):
        assert secret not in path.read_text(encoding="utf-8")


def test_failed_vault_write_keeps_previous_token_and_redacts_errors(
    tmp_path, credential_backend, monkeypatch
):
    secret = "github_pat_test_secret"
    app = create_app(tmp_path, github_token="startup_token")
    with TestClient(app) as client:

        def fail(*args):
            raise OSError(secret)

        monkeypatch.setattr(credential_backend, "set_password", fail)
        response = client.put(
            "/api/settings/github", json={"token": secret}, headers={"X-PWP-App": "1"}
        )
        assert response.status_code == 503
        assert secret not in response.text
        assert app.state.collector.github_token == "startup_token"
        assert not client.get("/api/settings/github").json()["persisted"]


def test_unavailable_vault_does_not_prevent_server_start(tmp_path, monkeypatch):
    def fail():
        raise OSError("private backend details")

    monkeypatch.setattr("app.credentials._backend", fail)
    with TestClient(create_app(tmp_path, github_token="startup_token")) as client:
        state = client.get("/api/settings/github").json()
        assert state["configured"]
        assert state["storage_error"] == "Cannot read the system credential store"
        assert "private backend details" not in str(state)


def test_saved_token_scope_and_startup_precedence(tmp_path):
    from app.credentials import TokenStore

    TokenStore(tmp_path / "one").save("saved_token")
    first = create_app(tmp_path / "one", github_token="startup_token")
    with TestClient(first) as client:
        assert first.state.collector.github_token == "saved_token"
        assert client.get("/api/settings/github").json()["persisted"]
    second = create_app(tmp_path / "two", github_token="startup_token")
    with TestClient(second):
        assert second.state.collector.github_token == "startup_token"
