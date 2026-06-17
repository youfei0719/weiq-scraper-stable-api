import importlib
import json
import sqlite3
import time
import os
from pathlib import Path

from fastapi.testclient import TestClient


def _load_api_module(monkeypatch, tmp_path):
    monkeypatch.setenv("WEIQ_DB_PATH", str(tmp_path / "weiq_local.db"))
    monkeypatch.setenv("WEIQ_API_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("WEIQ_AUTH_STATE_DIR", str(tmp_path / "runtime" / "auth_sessions"))
    monkeypatch.setenv("WEIQ_KEEP_AUTH_STATE_FOR_DEBUG", "true")
    monkeypatch.setenv("WEIQ_BROWSER_AUTH_MODE", os.environ.get("WEIQ_BROWSER_AUTH_MODE", "per_task"))
    monkeypatch.setenv("WEIQ_LEGACY_STATE_JSON", os.environ.get("WEIQ_LEGACY_STATE_JSON", str(tmp_path / "state.json")))
    monkeypatch.setenv("WEIQ_LEGACY_HEADLESS", os.environ.get("WEIQ_LEGACY_HEADLESS", "false"))
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


def test_legacy_state_mode_creates_task_without_login_session(monkeypatch, tmp_path):
    monkeypatch.setenv("WEIQ_BROWSER_AUTH_MODE", "legacy_state")
    monkeypatch.setenv("WEIQ_LEGACY_STATE_JSON", str(tmp_path / "state.json"))
    module = _load_api_module(monkeypatch, tmp_path)
    legacy_state_path = tmp_path / "state.json"
    _write_fake_storage_state(str(legacy_state_path))
    captured = {}

    def fake_run_crawl(config, hooks=None):
        captured["state_storage"] = config.state_storage
        captured["headless"] = config.headless
        captured["save_storage_state"] = config.save_storage_state
        output_excel = Path(config.output_excel)
        output_excel.parent.mkdir(parents=True, exist_ok=True)
        output_excel.write_bytes(b"legacy-excel")
        if hooks and hooks.on_status:
            hooks.on_status(
                status="SUCCESS",
                progress=1.0,
                processed_accounts=1,
                success_accounts=1,
                failed_accounts=0,
                skipped_accounts=0,
                total_accounts=1,
                message="ok",
            )
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
        response = client.post(
            "/v1/tasks/crawl",
            json={
                "accounts": [{"nickname": "A", "uid": "123"}],
                "headless": True,
                "retry_times": 1,
                "retry_backoff_seconds": 3,
                "resume": False,
            },
        )
        assert response.status_code == 200
        task_id = response.json()["task_id"]
        payload = _wait_for_task(client, task_id)
        assert payload["status"] == "SUCCESS"
        assert captured["state_storage"] == str(legacy_state_path)
        assert captured["headless"] is False
        assert captured["save_storage_state"] is True
    module._shutdown()


def test_legacy_state_mode_blocks_without_state_file(monkeypatch, tmp_path):
    monkeypatch.setenv("WEIQ_BROWSER_AUTH_MODE", "legacy_state")
    monkeypatch.setenv("WEIQ_LEGACY_STATE_JSON", str(tmp_path / "state.json"))
    module = _load_api_module(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        response = client.post(
            "/v1/tasks/crawl",
            json={
                "accounts": [{"nickname": "A", "uid": "123"}],
                "headless": True,
                "retry_times": 1,
                "retry_backoff_seconds": 3,
                "resume": False,
            },
        )
        assert response.status_code == 409
        assert "BLOCKED_AUTH" in response.json()["detail"]
    module._shutdown()


def test_legacy_open_login_and_check_save_state(monkeypatch, tmp_path):
    monkeypatch.setenv("WEIQ_BROWSER_AUTH_MODE", "legacy_state")
    monkeypatch.setenv("WEIQ_LEGACY_STATE_JSON", str(tmp_path / "state.json"))
    module = _load_api_module(monkeypatch, tmp_path)

    class DummyLocator:
        def __init__(self, result_count=0, body_text="已进入 WEIQ 控制台"):
            self._result_count = result_count
            self._body_text = body_text

        def count(self):
            return self._result_count

        def nth(self, index):  # noqa: ARG002
            return self

        def get_attribute(self, name):
            return ""

        def inner_text(self, timeout=0):  # noqa: ARG002
            return self._body_text

        @property
        def first(self):
            return self

    class DummyPage:
        url = "https://www.weiq.com/"

        def __init__(self):
            self.goto_calls = []

        def goto(self, url, timeout=0, wait_until=None):  # noqa: ARG002
            self.goto_calls.append(url)
            return object()

        def title(self):
            return "WEIQ"

        def locator(self, selector):
            if selector == "body":
                return DummyLocator(body_text="已进入 WEIQ 控制台")
            return DummyLocator(result_count=0)

        def content(self):
            return "已进入 WEIQ 控制台"

    class DummyContext:
        def __init__(self):
            self.storage_state_paths = []

        def new_page(self):
            return DummyPage()

        def storage_state(self, path):
            self.storage_state_paths.append(path)
            Path(path).write_text(
                json.dumps({"cookies": [{"name": "sid", "value": "1", "domain": ".weiq.com", "path": "/"}], "origins": []}),
                encoding="utf-8",
            )

        def close(self):
            return None

    class DummyBrowser:
        def __init__(self):
            self.context = DummyContext()

        def new_context(self):
            return self.context

        def close(self):
            return None

    class DummyChromium:
        def __init__(self):
            self.launched_kwargs = None
            self.browser = DummyBrowser()

        def launch(self, **kwargs):
            self.launched_kwargs = kwargs
            return self.browser

    class DummyPlaywright:
        def __init__(self):
            self.chromium = DummyChromium()
            self.stopped = False

        def stop(self):
            self.stopped = True

    class DummyPlaywrightStarter:
        def start(self):
            return DummyPlaywright()

    monkeypatch.setattr(module, "sync_playwright", lambda: DummyPlaywrightStarter())

    with TestClient(module.app) as client:
        open_response = client.post("/v1/auth/legacy/open-login")
        assert open_response.status_code == 200
        open_payload = open_response.json()
        assert open_payload["headless"] is False
        assert open_payload["state_json_path"].endswith("state.json")

        check_response = client.post("/v1/auth/legacy/check")
        assert check_response.status_code == 200
        check_payload = check_response.json()
        assert check_payload["authenticated"] is True
        assert check_payload["state_json_exists"] is True
        assert Path(check_payload["state_json_path"]).exists()
    module._shutdown()


def test_legacy_check_returns_blocked_when_page_is_blocked(monkeypatch, tmp_path):
    monkeypatch.setenv("WEIQ_BROWSER_AUTH_MODE", "legacy_state")
    monkeypatch.setenv("WEIQ_LEGACY_STATE_JSON", str(tmp_path / "state.json"))
    module = _load_api_module(monkeypatch, tmp_path)

    class DummyLocator:
        def count(self):
            return 0

        def nth(self, index):  # noqa: ARG002
            return self

        def get_attribute(self, name):
            return ""

        def inner_text(self, timeout=0):  # noqa: ARG002
            return "The URL you requested has been blocked"

        @property
        def first(self):
            return self

    class DummyPage:
        url = "https://www.weiq.com/"

        def title(self):
            return "The URL you requested has been blocked"

        def goto(self, url, timeout=0, wait_until=None):  # noqa: ARG002
            return object()

        def locator(self, selector):
            return DummyLocator()

        def content(self):
            return "blocked"

    class DummyContext:
        def new_page(self):
            return DummyPage()

        def storage_state(self, path):
            raise AssertionError("blocked 页面不应保存 state.json")

        def close(self):
            return None

    class DummyBrowser:
        def new_context(self):
            return DummyContext()

        def close(self):
            return None

    class DummyChromium:
        def launch(self, **kwargs):  # noqa: ANN003
            return DummyBrowser()

    class DummyPlaywright:
        def __init__(self):
            self.chromium = DummyChromium()

        def stop(self):
            return None

    class DummyPlaywrightStarter:
        def start(self):
            return DummyPlaywright()

    monkeypatch.setattr(module, "sync_playwright", lambda: DummyPlaywrightStarter())

    with TestClient(module.app) as client:
        client.post("/v1/auth/legacy/open-login")
        check_response = client.post("/v1/auth/legacy/check")
        assert check_response.status_code == 200
        payload = check_response.json()
        assert payload["authenticated"] is False
        assert payload["blocked"] is True
        assert "拦截" in payload["message"] or "blocked" in payload["message"].lower()
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


def test_blocked_page_is_detected(monkeypatch, tmp_path):
    module = _load_api_module(monkeypatch, tmp_path)
    assert module._detect_blocked_text(title="The URL you requested has been blocked", body="")
    assert module._detect_blocked_text(title="", body="some body says blocked by upstream")
    module._shutdown()


def test_submit_login_returns_clear_blocked_error(monkeypatch, tmp_path):
    module = _load_api_module(monkeypatch, tmp_path)
    session = {
        "session_id": "sess-1",
        "state_storage": str(tmp_path / "runtime" / "auth_sessions" / "sess-1" / "storage_state.json"),
    }

    class DummyPlaywrightContext:
        def __enter__(self):
            return object()

        def __exit__(self, exc_type, exc, tb):
            return False

    class DummyResponse:
        status = 200

    class DummyLocator:
        def inner_text(self, timeout=0):
            return "The URL you requested has been blocked"

        def count(self):
            return 0

        @property
        def first(self):
            return self

    class DummyPage:
        url = "https://www.weiq.com/"

        def goto(self, *args, **kwargs):
            return DummyResponse()

        def title(self):
            return "The URL you requested has been blocked"

        def locator(self, selector):
            return DummyLocator()

        def screenshot(self, path, full_page=True):
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_bytes(b"png")

        def content(self):
            return "blocked"

    class DummyContext:
        def storage_state(self, path):
            Path(path).write_text("{}", encoding="utf-8")

    class DummyBrowser:
        def close(self):
            return None

    monkeypatch.setattr(module, "sync_playwright", lambda: DummyPlaywrightContext())
    monkeypatch.setattr(module, "init_browser", lambda p, headless=True, state_storage=None: (DummyBrowser(), DummyContext(), DummyPage()))

    status, message = module._submit_auth_session_with_browser(
        session,
        {"login_type": "password", "username": "demo", "password": "demo"},
        module.load_settings(),
    )
    assert status == "failed"
    assert "当前服务器出口访问 WEIQ 被拦截" in message
    assert (tmp_path / "runtime" / "auth_sessions" / "sess-1" / "login_failed.png").exists()
    module._shutdown()


def test_debug_weiq_access_does_not_leak_proxy_password(monkeypatch, tmp_path):
    monkeypatch.setenv("WEIQ_PROXY_SERVER", "http://proxy.example.com:8080")
    monkeypatch.setenv("WEIQ_PROXY_USERNAME", "proxy-user")
    monkeypatch.setenv("WEIQ_PROXY_PASSWORD", "super-secret")
    module = _load_api_module(monkeypatch, tmp_path)
    monkeypatch.setattr(
        module,
        "_requests_weiq_access_probe",
        lambda: {
            "ok": False,
            "status_code": 403,
            "final_url": "https://www.weiq.com/",
            "title_detected": "The URL you requested has been blocked",
            "blocked_detected": True,
            "error": None,
        },
    )
    monkeypatch.setattr(
        module,
        "_playwright_weiq_access_probe",
        lambda runtime_dir: {
            "ok": False,
            "status_code": 403,
            "final_url": "https://www.weiq.com/",
            "title": "The URL you requested has been blocked",
            "blocked_detected": True,
            "screenshot_path": str(Path(runtime_dir) / "debug" / "weiq_access_test.png"),
            "error": None,
        },
    )
    monkeypatch.setattr(module, "_detect_public_ip", lambda: "203.0.113.10")
    with TestClient(module.app) as client:
        response = client.get("/v1/debug/weiq-access")
        assert response.status_code == 200
        payload = response.json()
        encoded = json.dumps(payload, ensure_ascii=False)
        assert "super-secret" not in encoded
        assert payload["egress"]["proxy_enabled"] is True
        assert payload["egress"]["proxy_server"] == "http://proxy.example.com:8080"
    module._shutdown()
