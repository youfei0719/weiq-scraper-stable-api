import importlib
import json
import sqlite3
import time
from pathlib import Path

from fastapi.testclient import TestClient


def _load_api_module(monkeypatch, tmp_path):
    monkeypatch.setenv("WEIQ_DB_PATH", str(tmp_path / "weiq_local.db"))
    monkeypatch.setenv("WEIQ_API_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("WEIQ_AUTH_STATE_DIR", str(tmp_path / "runtime" / "auth_sessions"))
    monkeypatch.setenv("WEIQ_KEEP_AUTH_STATE_FOR_DEBUG", "true")
    import cloud_api

    module = importlib.reload(cloud_api)
    return module


def _db_row(db_path: Path, sql: str, params=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _write_fake_storage_state(path: str):
    Path(path).write_text(
        json.dumps(
            {
                "cookies": [{"name": "sid", "value": "1", "domain": ".weiq.com", "path": "/"}],
                "origins": [],
            }
        ),
        encoding="utf-8",
    )


def _create_authenticated_session(client: TestClient, module, tmp_path: Path) -> dict:
    response = client.post("/v1/auth/session", json={})
    assert response.status_code == 200
    session_id = response.json()["session_id"]
    row = _db_row(tmp_path / "weiq_local.db", "SELECT * FROM auth_sessions WHERE session_id = ?", (session_id,))
    _write_fake_storage_state(row["state_storage"])
    check = client.post(f"/v1/auth/session/{session_id}/check")
    assert check.status_code == 200
    assert check.json()["status"] == "authenticated"
    return row


def _wait_for_task(client: TestClient, task_id: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = client.get(f"/v1/tasks/{task_id}")
        payload = response.json()
        if payload["status"] not in {"PENDING", "RUNNING"}:
            return payload
        time.sleep(0.05)
    raise AssertionError("task did not finish in time")


def test_auth_session_create(monkeypatch, tmp_path):
    module = _load_api_module(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        response = client.post("/v1/auth/session", json={})
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "waiting_credentials"
        row = _db_row(tmp_path / "weiq_local.db", "SELECT * FROM auth_sessions WHERE session_id = ?", (payload["session_id"],))
        assert row is not None
        assert Path(row["state_storage"]).parent.exists()
    module._shutdown()


def test_auth_session_check_without_storage(monkeypatch, tmp_path):
    module = _load_api_module(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        session_id = client.post("/v1/auth/session", json={}).json()["session_id"]
        response = client.post(f"/v1/auth/session/{session_id}/check")
        assert response.status_code == 200
        assert response.json()["status"] == "waiting_credentials"
    module._shutdown()


def test_create_task_requires_authenticated_session(monkeypatch, tmp_path):
    module = _load_api_module(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        session_id = client.post("/v1/auth/session", json={}).json()["session_id"]
        response = client.post(
            "/v1/tasks/crawl",
            json={
                "accounts": [{"nickname": "A", "uid": "123"}],
                "login_session_id": session_id,
                "headless": True,
                "retry_times": 1,
                "retry_backoff_seconds": 3,
                "resume": False,
            },
        )
        assert response.status_code == 400
    module._shutdown()


def test_create_task_with_authenticated_session(monkeypatch, tmp_path):
    module = _load_api_module(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        session = _create_authenticated_session(client, module, tmp_path)
        response = client.post(
            "/v1/tasks/crawl",
            json={
                "accounts": [{"nickname": "A", "uid": "123", "account_id": "acc-1"}],
                "login_session_id": session["session_id"],
                "headless": True,
                "retry_times": 1,
                "retry_backoff_seconds": 3,
                "resume": False,
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["task_id"]
        row = _db_row(tmp_path / "weiq_local.db", "SELECT * FROM tasks WHERE task_id = ?", (payload["task_id"],))
        assert row is not None
        assert row["login_session_id"] == session["session_id"]
    module._shutdown()


def test_worker_uses_session_storage_state(monkeypatch, tmp_path):
    module = _load_api_module(monkeypatch, tmp_path)
    captured = {}

    def fake_run_crawl(config, hooks=None):
        captured["state_storage"] = config.state_storage
        output_excel = Path(config.output_excel)
        output_excel.parent.mkdir(parents=True, exist_ok=True)
        output_excel.write_bytes(b"fake")
        if hooks and hooks.on_status:
            hooks.on_status(status="SUCCESS", progress=1.0, processed_accounts=1, success_accounts=1, failed_accounts=0, skipped_accounts=0, total_accounts=1, message="ok")
        return module.CrawlResult(
            status=module.TaskStatus.SUCCESS,
            output_excel=str(output_excel),
            total_accounts=1,
            processed_accounts=1,
            success_accounts=1,
            failed_accounts=0,
            skipped_accounts=0,
            message="ok",
        )

    monkeypatch.setattr(module, "run_crawl", fake_run_crawl)
    with TestClient(module.app) as client:
        session = _create_authenticated_session(client, module, tmp_path)
        response = client.post(
            "/v1/tasks/crawl",
            json={
                "accounts": [{"nickname": "A", "uid": "123"}],
                "login_session_id": session["session_id"],
                "headless": True,
                "retry_times": 1,
                "retry_backoff_seconds": 3,
                "resume": False,
            },
        )
        task_id = response.json()["task_id"]
        payload = _wait_for_task(client, task_id)
        assert payload["status"] == "SUCCESS"
        assert captured["state_storage"] == session["state_storage"]
    module._shutdown()


def test_export_returns_excel(monkeypatch, tmp_path):
    module = _load_api_module(monkeypatch, tmp_path)

    def fake_run_crawl(config, hooks=None):
        output_excel = Path(config.output_excel)
        output_excel.parent.mkdir(parents=True, exist_ok=True)
        output_excel.write_bytes(b"excel-bytes")
        return module.CrawlResult(
            status=module.TaskStatus.SUCCESS,
            output_excel=str(output_excel),
            total_accounts=1,
            processed_accounts=1,
            success_accounts=1,
            failed_accounts=0,
            skipped_accounts=0,
            message="ok",
        )

    monkeypatch.setattr(module, "run_crawl", fake_run_crawl)
    with TestClient(module.app) as client:
        session = _create_authenticated_session(client, module, tmp_path)
        task_id = client.post(
            "/v1/tasks/crawl",
            json={
                "accounts": [{"nickname": "A", "uid": "123"}],
                "login_session_id": session["session_id"],
                "headless": True,
                "retry_times": 1,
                "retry_backoff_seconds": 3,
                "resume": False,
            },
        ).json()["task_id"]
        _wait_for_task(client, task_id)
        export = client.get(f"/v1/tasks/{task_id}/export")
        assert export.status_code == 200
        assert export.content == b"excel-bytes"
    module._shutdown()


def test_debug_env(monkeypatch, tmp_path):
    module = _load_api_module(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        response = client.get("/v1/debug/env")
        assert response.status_code == 200
        payload = response.json()
        assert payload["db_path"].endswith("weiq_local.db")
        assert "auth_sessions_columns" in payload
        assert "tasks_columns" in payload
        assert payload["runtime_dir"].endswith("runtime")
        assert payload["auth_state_dir"].endswith("auth_sessions")
    module._shutdown()
