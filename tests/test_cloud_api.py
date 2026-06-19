import os
import sys
import time
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

if "playwright.sync_api" not in sys.modules:
    playwright_module = types.ModuleType("playwright")
    playwright_sync_api_module = types.ModuleType("playwright.sync_api")
    playwright_async_api_module = types.ModuleType("playwright.async_api")

    class _PlaywrightTimeoutError(Exception):
        pass

    def _sync_playwright():  # noqa: ANN202
        raise RuntimeError("playwright is not installed in this test environment")

    def _async_playwright():  # noqa: ANN202
        raise RuntimeError("playwright is not installed in this test environment")

    playwright_sync_api_module.TimeoutError = _PlaywrightTimeoutError
    playwright_sync_api_module.sync_playwright = _sync_playwright
    playwright_async_api_module.async_playwright = _async_playwright
    playwright_module.sync_api = playwright_sync_api_module
    playwright_module.async_api = playwright_async_api_module
    sys.modules["playwright"] = playwright_module
    sys.modules["playwright.sync_api"] = playwright_sync_api_module
    sys.modules["playwright.async_api"] = playwright_async_api_module

import cloud_api


class _FakeProc:
    def __init__(self):
        self._running = True

    def poll(self):
        return None if self._running else 0

    def terminate(self):
        self._running = False

    def wait(self, timeout=None):  # noqa: ANN001
        self._running = False
        return 0

    def kill(self):
        self._running = False


class _FakePage:
    url = "https://www.weiq.com/"

    def goto(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None

    def title(self):
        return "WEIQ"


class _FakeContext:
    def __init__(self):
        self.pages = [_FakePage()]
        self.closed = False

    def new_page(self):
        return self.pages[0]

    def storage_state(self, path: str):
        Path(path).write_text(json.dumps({"cookies": [{"name": "sid"}], "origins": []}), encoding="utf-8")

    def close(self):
        self.closed = True


class _FakeBlockedPage:
    def __init__(self, url: str = "https://www.weiq.com/security", title: str = "安全验证"):
        self.url = url
        self._title = title

    def title(self):
        return self._title

    def screenshot(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return b"fake-png"


class _FakeChromium:
    def launch_persistent_context(self, **kwargs):  # noqa: ANN003
        return _FakeContext()


class _FakePlaywright:
    def __init__(self):
        self.chromium = _FakeChromium()
        self.stopped = False

    def stop(self):
        self.stopped = True


class _FakePlaywrightManager:
    def __init__(self):
        self.instance = _FakePlaywright()

    def start(self):
        return self.instance


class _AsyncFakeLocator:
    def __init__(self, text: str = "WEIQ 控制台", count: int = 0):
        self.text = text
        self._count = count

    async def inner_text(self, timeout=0):  # noqa: ANN001, ARG002
        return self.text

    async def count(self):
        return self._count


class _AsyncFakePage:
    url = "https://www.weiq.com/console"

    async def goto(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None

    async def title(self):
        return "WEIQ 控制台"

    def locator(self, selector: str):
        return _AsyncFakeLocator(count=0 if "input" in selector else 0)


class _AsyncFakeBlockedPage(_AsyncFakePage):
    url = "https://www.weiq.com/security"

    async def title(self):
        return "安全验证"

    def locator(self, selector: str):
        return _AsyncFakeLocator(text="WEIQ 安全验证", count=1 if "input" in selector else 0)


class _AsyncFakeContext:
    def __init__(self):
        self.pages = [_AsyncFakePage()]
        self.closed = False

    async def new_page(self):
        return self.pages[0]

    async def storage_state(self, path: str):
        Path(path).write_text(json.dumps({"cookies": [{"name": "sid"}], "origins": []}), encoding="utf-8")

    async def close(self):
        self.closed = True


class _AsyncFakeChromium:
    async def launch_persistent_context(self, **kwargs):  # noqa: ANN003
        return _AsyncFakeContext()


class _AsyncFakePlaywright:
    def __init__(self):
        self.chromium = _AsyncFakeChromium()
        self.stopped = False

    async def stop(self):
        self.stopped = True


class _AsyncFakePlaywrightManager:
    def __init__(self):
        self.instance = _AsyncFakePlaywright()

    async def start(self):
        return self.instance


class TestCloudAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.path.exists(cloud_api.DB_PATH):
            os.remove(cloud_api.DB_PATH)

    def setUp(self):
        if os.path.exists(cloud_api.DB_PATH):
            os.remove(cloud_api.DB_PATH)
        while not cloud_api.TASK_QUEUE.empty():
            try:
                cloud_api.TASK_QUEUE.get_nowait()
                cloud_api.TASK_QUEUE.task_done()
            except Exception:
                break
        with cloud_api.QUEUE_LOCK:
            cloud_api.QUEUED_TASK_IDS.clear()
            cloud_api.ACTIVE_TASK_IDS.clear()
        with cloud_api.BROWSER_WORKER_LOCK:
            cloud_api.BROWSER_WORKER_RUNTIME.clear()
        cloud_api.BROWSER_WORKER_CONTROLLER._state = {
            "runtime_state": "closed",
            "browser_session_running": False,
            "live_auth_verified": False,
        }
        cloud_api.init_db()

    def test_health(self):
        with TestClient(cloud_api.app) as client:
            resp = client.get("/health")
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["status"], "ok")

    def test_browser_auth_status_reports_missing_state_in_browser_worker_mode(self):
        with patch.dict(
            os.environ,
            {
                "WEIQ_BROWSER_AUTH_MODE": "browser_worker",
                "WEIQ_LEGACY_STATE_JSON": str(Path(cloud_api.get_runtime_dir()) / "missing-state.json"),
                "WEIQ_BROWSER_USER_DATA_DIR": str(Path(cloud_api.get_runtime_dir()) / "browser-profile"),
                "WEIQ_BROWSER_HEADLESS": "false",
            },
            clear=False,
        ):
            with TestClient(cloud_api.app) as client:
                resp = client.get("/v1/auth/browser/status")

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertFalse(body["authenticated"])
        self.assertFalse(body["state_json_exists"])
        self.assertEqual(body["auth_mode"], "browser_worker")

    def test_create_task_returns_auth_required_when_browser_worker_has_no_login_state(self):
        with patch.dict(
            os.environ,
            {
                "WEIQ_BROWSER_AUTH_MODE": "browser_worker",
                "WEIQ_LEGACY_STATE_JSON": str(Path(cloud_api.get_runtime_dir()) / "missing-state.json"),
                "WEIQ_BROWSER_USER_DATA_DIR": str(Path(cloud_api.get_runtime_dir()) / "browser-profile"),
                "WEIQ_BROWSER_HEADLESS": "false",
            },
            clear=False,
        ):
            with TestClient(cloud_api.app) as client:
                resp = client.post(
                    "/v1/tasks/crawl",
                    json={"accounts": [{"nickname": "测试账号", "uid": "1234567890"}]},
                )

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "AUTH_REQUIRED")
        self.assertIsNone(body.get("task_id"))

    def test_browser_auth_check_marks_authenticated_when_state_exists(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            state_path.write_text(json.dumps({"cookies": [{"name": "sid"}], "origins": []}), encoding="utf-8")
            user_data_dir = Path(temp_dir) / "profile"
            with patch.dict(
                os.environ,
                {
                    "WEIQ_BROWSER_AUTH_MODE": "browser_worker",
                    "WEIQ_LEGACY_STATE_JSON": str(state_path),
                    "WEIQ_BROWSER_USER_DATA_DIR": str(user_data_dir),
                    "WEIQ_BROWSER_HEADLESS": "false",
                },
                clear=False,
            ):
                with TestClient(cloud_api.app) as client:
                    resp = client.post("/v1/auth/browser/check")

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["authenticated"])
        self.assertEqual(body["login_status"], "saved_state_unverified")
        self.assertFalse(body["live_auth_verified"])

    def test_browser_auth_status_separates_login_state_from_display_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            state_path.write_text(json.dumps({"cookies": [{"name": "sid"}], "origins": []}), encoding="utf-8")
            user_data_dir = Path(temp_dir) / "profile"
            with patch.dict(
                os.environ,
                {
                    "WEIQ_BROWSER_AUTH_MODE": "browser_worker",
                    "WEIQ_LEGACY_STATE_JSON": str(state_path),
                    "WEIQ_BROWSER_USER_DATA_DIR": str(user_data_dir),
                    "WEIQ_BROWSER_HEADLESS": "false",
                    "WEIQ_BROWSER_DISPLAY": ":99",
                },
                clear=False,
            ):
                with TestClient(cloud_api.app) as client:
                    resp = client.get("/v1/auth/browser/status")

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["authenticated"])
        self.assertFalse(body["display_ready"])
        self.assertEqual(body["browser_display"], ":99")
        self.assertIn("尚未", body["message"])

    def test_browser_auth_open_creates_runtime_with_novnc_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            user_data_dir = Path(temp_dir) / "profile"
            novnc_dir = Path(temp_dir) / "noVNC"
            novnc_dir.mkdir(parents=True, exist_ok=True)
            (novnc_dir / "vnc.html").write_text("ok", encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "WEIQ_BROWSER_AUTH_MODE": "browser_worker",
                    "WEIQ_LEGACY_STATE_JSON": str(state_path),
                    "WEIQ_BROWSER_USER_DATA_DIR": str(user_data_dir),
                    "WEIQ_NOVNC_WEB_DIR": str(novnc_dir),
                    "WEIQ_BROWSER_SESSION_TTL_SECONDS": "600",
                },
                clear=False,
            ), patch("cloud_api._ensure_browser_worker_support_files"), patch(
                "cloud_api._launch_local_process",
                side_effect=[_FakeProc(), _FakeProc(), _FakeProc()],
            ), patch("cloud_api._wait_for_local_port"), patch(
                "cloud_api.async_playwright",
                return_value=_AsyncFakePlaywrightManager(),
            ):
                with TestClient(cloud_api.app) as client:
                    resp = client.post("/v1/auth/browser/open")

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["browser_session_running"])
        self.assertTrue(body["novnc_running"])
        self.assertTrue(body["novnc_local_url"].startswith("http://127.0.0.1:6080/"))
        self.assertEqual(body["novnc_proxy_path"], "/browser-session/")
        self.assertTrue(body["session_id"])

    def test_browser_auth_open_reuses_existing_display_runtime(self):
        runtime = {
            "session_id": None,
            "playwright": None,
            "context": None,
            "page": None,
            "display": ":99",
            "runtime_env": {"DISPLAY": ":99"},
            "vnc_port": 5901,
            "novnc_port": 6080,
            "novnc_local_url": "http://127.0.0.1:6080/vnc.html?path=websockify",
            "xvfb_proc": 123,
            "x11vnc_proc": 456,
            "websockify_proc": 789,
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            user_data_dir = Path(temp_dir) / "profile"
            novnc_dir = Path(temp_dir) / "noVNC"
            novnc_dir.mkdir(parents=True, exist_ok=True)
            (novnc_dir / "vnc.html").write_text("ok", encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "WEIQ_BROWSER_AUTH_MODE": "browser_worker",
                    "WEIQ_LEGACY_STATE_JSON": str(state_path),
                    "WEIQ_BROWSER_USER_DATA_DIR": str(user_data_dir),
                    "WEIQ_NOVNC_WEB_DIR": str(novnc_dir),
                },
                clear=False,
            ), patch("cloud_api._ensure_browser_worker_support_files"), patch(
                "cloud_api._browser_worker_runtime_snapshot",
                return_value=runtime,
            ), patch(
                "cloud_api._is_process_running",
                return_value=True,
            ), patch("cloud_api._is_display_ready", return_value=True), patch(
                "cloud_api.async_playwright",
                return_value=_AsyncFakePlaywrightManager(),
            ), patch("cloud_api._launch_local_process") as launch_mock:
                with TestClient(cloud_api.app) as client:
                    resp = client.post("/v1/auth/browser/open")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(launch_mock.call_count, 0)
        body = resp.json()
        self.assertTrue(body["browser_session_running"])
        self.assertTrue(body["display_ready"])

    def test_browser_auth_close_stops_runtime(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            user_data_dir = Path(temp_dir) / "profile"
            novnc_dir = Path(temp_dir) / "noVNC"
            novnc_dir.mkdir(parents=True, exist_ok=True)
            (novnc_dir / "vnc.html").write_text("ok", encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "WEIQ_BROWSER_AUTH_MODE": "browser_worker",
                    "WEIQ_LEGACY_STATE_JSON": str(state_path),
                    "WEIQ_BROWSER_USER_DATA_DIR": str(user_data_dir),
                    "WEIQ_NOVNC_WEB_DIR": str(novnc_dir),
                },
                clear=False,
            ), patch("cloud_api._ensure_browser_worker_support_files"), patch(
                "cloud_api._launch_local_process",
                side_effect=[_FakeProc(), _FakeProc(), _FakeProc()],
            ), patch("cloud_api._wait_for_local_port"), patch(
                "cloud_api.async_playwright",
                return_value=_AsyncFakePlaywrightManager(),
            ):
                with TestClient(cloud_api.app) as client:
                    open_resp = client.post("/v1/auth/browser/open")
                    self.assertEqual(open_resp.status_code, 200)
                    close_resp = client.post("/v1/auth/browser/close")

        self.assertEqual(close_resp.status_code, 200)
        close_body = close_resp.json()
        self.assertFalse(close_body["browser_session_running"])
        self.assertFalse(close_body["novnc_running"])

    def test_browser_auth_can_reopen_after_close(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            novnc_dir = Path(temp_dir) / "noVNC"
            novnc_dir.mkdir(parents=True, exist_ok=True)
            (novnc_dir / "vnc.html").write_text("ok", encoding="utf-8")
            managers = [_AsyncFakePlaywrightManager(), _AsyncFakePlaywrightManager()]
            with patch.dict(
                os.environ,
                {
                    "WEIQ_BROWSER_AUTH_MODE": "browser_worker",
                    "WEIQ_LEGACY_STATE_JSON": str(state_path),
                    "WEIQ_BROWSER_USER_DATA_DIR": str(Path(temp_dir) / "profile"),
                    "WEIQ_NOVNC_WEB_DIR": str(novnc_dir),
                },
                clear=False,
            ), patch("cloud_api._ensure_browser_worker_support_files"), patch(
                "cloud_api._launch_local_process",
                side_effect=[_FakeProc() for _ in range(6)],
            ), patch("cloud_api._wait_for_local_port"), patch(
                "cloud_api.async_playwright",
                side_effect=managers,
            ):
                with TestClient(cloud_api.app) as client:
                    first = client.post("/v1/auth/browser/open")
                    first_session = first.json()["session_id"]
                    closed = client.post("/v1/auth/browser/close")
                    second = client.post("/v1/auth/browser/open")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(closed.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertNotEqual(first_session, second.json()["session_id"])
        self.assertTrue(second.json()["browser_session_running"])

    def test_browser_auth_start_failure_returns_structured_503_and_cleans_processes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            novnc_dir = Path(temp_dir) / "noVNC"
            novnc_dir.mkdir(parents=True, exist_ok=True)
            (novnc_dir / "vnc.html").write_text("ok", encoding="utf-8")
            processes = [_FakeProc() for _ in range(3)]
            with patch.dict(
                os.environ,
                {
                    "WEIQ_BROWSER_AUTH_MODE": "browser_worker",
                    "WEIQ_BROWSER_USER_DATA_DIR": str(Path(temp_dir) / "profile"),
                    "WEIQ_NOVNC_WEB_DIR": str(novnc_dir),
                },
                clear=False,
            ), patch("cloud_api._ensure_browser_worker_support_files"), patch(
                "cloud_api._launch_local_process",
                side_effect=processes,
            ), patch("cloud_api._wait_for_local_port"), patch(
                "cloud_api.async_playwright",
                side_effect=RuntimeError("playwright failed"),
            ):
                with TestClient(cloud_api.app) as client:
                    response = client.post("/v1/auth/browser/open")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "BROWSER_WORKER_START_FAILED")
        self.assertEqual(response.json()["detail"]["stage"], "playwright")
        self.assertTrue(all(proc.poll() is not None for proc in processes))

    def test_browser_auth_check_does_not_leak_state_or_cookie_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            user_data_dir = Path(temp_dir) / "profile"
            novnc_dir = Path(temp_dir) / "noVNC"
            novnc_dir.mkdir(parents=True, exist_ok=True)
            (novnc_dir / "vnc.html").write_text("ok", encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "WEIQ_BROWSER_AUTH_MODE": "browser_worker",
                    "WEIQ_LEGACY_STATE_JSON": str(state_path),
                    "WEIQ_BROWSER_USER_DATA_DIR": str(user_data_dir),
                    "WEIQ_NOVNC_WEB_DIR": str(novnc_dir),
                },
                clear=False,
            ), patch("cloud_api._ensure_browser_worker_support_files"), patch(
                "cloud_api._launch_local_process",
                side_effect=[_FakeProc(), _FakeProc(), _FakeProc()],
            ), patch("cloud_api._wait_for_local_port"), patch(
                "cloud_api.async_playwright",
                return_value=_AsyncFakePlaywrightManager(),
            ):
                with TestClient(cloud_api.app) as client:
                    client.post("/v1/auth/browser/open")
                    resp = client.post("/v1/auth/browser/check")

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertNotIn("state_json", body)
        self.assertNotIn("cookies", body)
        self.assertIn("authenticated", body)

    def test_browser_auth_status_expires_runtime(self):
        proc = _FakeProc()
        cloud_api.BROWSER_WORKER_RUNTIME["session"] = {
            "session_id": "expired-session",
            "expires_at": "2000-01-01T00:00:00",
            "websockify_proc": proc,
            "x11vnc_proc": proc,
            "xvfb_proc": proc,
        }
        with patch.dict(
            os.environ,
            {
                "WEIQ_BROWSER_AUTH_MODE": "browser_worker",
                "WEIQ_LEGACY_STATE_JSON": str(Path(tempfile.gettempdir()) / "missing-state.json"),
            },
            clear=False,
        ):
            with TestClient(cloud_api.app) as client:
                resp = client.get("/v1/auth/browser/status")

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertFalse(body["browser_session_running"])

    def test_task_lifecycle_minimal(self):
        with patch("cloud_api.enqueue_task", return_value=True), patch("cloud_api.run_crawl", side_effect=RuntimeError("boom")):
            with TestClient(cloud_api.app) as client:
                create_resp = client.post(
                    "/v1/tasks/crawl",
                    json={
                        "input_excel": "not_exists.xlsx",
                        "output_excel": "weiq_results.xlsx",
                        "output_dir": ".",
                    },
                )
                self.assertEqual(create_resp.status_code, 200)
                task_id = create_resp.json()["task_id"]
                with patch("cloud_api.uuid4") as uuid_mock, patch(
                    "cloud_api._build_eager_auth_session_state",
                ) as eager_mock:
                    uuid_mock.return_value.hex = "sess-lifecycle-1"
                    eager_mock.return_value = {
                        "session_id": "sess-lifecycle-1",
                        "task_id": task_id,
                        "status": "waiting_credentials",
                        "state_storage": cloud_api.build_auth_session_paths("sess-lifecycle-1")["state_storage"],
                        "message": "等待登录",
                        "expires_at": cloud_api.expiry_iso(),
                    }
                    cloud_api.run_task(task_id)
                task_resp = client.get(f"/v1/tasks/{task_id}")
                self.assertEqual(task_resp.status_code, 200)
                self.assertIn(task_resp.json()["status"], {"FAILED", "SUCCESS", "CANCELLED", "BLOCKED_AUTH"})

                cancel_resp = client.post(f"/v1/tasks/{task_id}/cancel")
                self.assertEqual(cancel_resp.status_code, 200)

    def test_auth_session_endpoints_minimal(self):
        with patch("cloud_api.ensure_auth_session_runtime", side_effect=lambda session_id: cloud_api.fetch_auth_session(session_id)):
            with TestClient(cloud_api.app) as client:
                create_resp = client.post("/v1/auth/session")
                self.assertEqual(create_resp.status_code, 200)
                session = create_resp.json()
                self.assertEqual(session["status"], "waiting_credentials")
                self.assertIn("password", session["available_login_types"])
                self.assertIn("phone_code", session["available_login_types"])

                get_resp = client.get(f"/v1/auth/session/{session['session_id']}")
                self.assertEqual(get_resp.status_code, 200)
                self.assertEqual(get_resp.json()["session_id"], session["session_id"])

                submit_resp = client.post(
                    f"/v1/auth/session/{session['session_id']}/submit",
                    json={"login_type": "password", "username": "demo", "password": "demo"},
                )
                self.assertEqual(submit_resp.status_code, 409)

    def test_create_auth_session_uses_per_task_state_storage_directory(self):
        with TestClient(cloud_api.app) as client:
            create_resp = client.post("/v1/auth/session")
            self.assertEqual(create_resp.status_code, 200)
            session = create_resp.json()
        row = cloud_api.fetch_auth_session(session["session_id"])
        assert row is not None
        state_storage = str(row["state_storage"])
        self.assertIn(f"auth_sessions/{session['session_id']}/storage_state.json", state_storage)
        self.assertTrue(Path(state_storage).parent.exists())

    def test_create_auth_session_eager_binds_task_and_returns_waiting_state(self):
        with patch("cloud_api.enqueue_task", return_value=True):
            task = cloud_api.create_task(
                cloud_api.CreateTaskRequest(
                    accounts=[cloud_api.AccountInput(nickname="测试账号", uid="1234567890")],
                )
            )

        eager_row = {
            "session_id": "sess-eager-1",
            "task_id": task.task_id,
            "status": "waiting_credentials",
            "login_url": "https://www.weiq.com/",
            "qr_image_base64": "preview-1",
            "message": "请先登录",
            "expires_at": cloud_api.expiry_iso(),
        }

        with patch("cloud_api.uuid4") as uuid_mock, patch(
            "cloud_api._build_eager_auth_session_state",
            return_value=eager_row,
        ), patch("cloud_api.start_worker"), patch("cloud_api.recover_incomplete_tasks"):
            uuid_mock.return_value.hex = "sess-eager-1"
            with TestClient(cloud_api.app) as client:
                resp = client.post("/v1/auth/session", json={"task_id": task.task_id, "eager": True})

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["session_id"], "sess-eager-1")
        self.assertEqual(body["status"], "waiting_credentials")
        task_row = cloud_api.fetch_one("SELECT login_session_id FROM tasks WHERE task_id = ?", (task.task_id,))
        assert task_row is not None
        self.assertEqual(task_row["login_session_id"], "sess-eager-1")

    def test_check_auth_session_uses_current_session_storage_and_requeues_task(self):
        with patch("cloud_api.enqueue_task", return_value=True):
            task = cloud_api.create_task(
                cloud_api.CreateTaskRequest(
                    accounts=[cloud_api.AccountInput(nickname="测试账号", uid="1234567890")],
                )
            )
        session_id = "sess-check-1"
        paths = cloud_api.build_auth_session_paths(session_id)
        Path(paths["state_storage"]).write_text(json.dumps({"cookies": [{"name": "sid"}], "origins": []}), encoding="utf-8")
        cloud_api.upsert_auth_session(
            session_id,
            {
                "task_id": task.task_id,
                "status": "waiting_credentials",
                "state_storage": paths["state_storage"],
                "message": "等待登录",
                "expires_at": cloud_api.expiry_iso(),
            },
        )
        cloud_api.upsert_task_event(task.task_id, {"login_session_id": session_id, "status": cloud_api.TaskStatus.BLOCKED_AUTH})

        with TestClient(cloud_api.app) as client, patch("cloud_api.enqueue_task", return_value=True):
            resp = client.post(f"/v1/auth/session/{session_id}/check")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "authenticated")
        row = cloud_api.fetch_auth_session(session_id)
        assert row is not None
        self.assertEqual(row["status"], "authenticated")
        task_row = cloud_api.fetch_one("SELECT status FROM tasks WHERE task_id = ?", (task.task_id,))
        assert task_row is not None
        self.assertEqual(task_row["status"], cloud_api.TaskStatus.PENDING)

    def test_inspect_active_auth_session_requires_usable_storage_state(self):
        class _Locator:
            def inner_text(self, timeout: int = 1500) -> str:  # noqa: ARG002
                return "欢迎来到 WEIQ"

        class _Page:
            url = "https://www.weiq.com/"

            def locator(self, selector: str) -> _Locator:  # noqa: ARG002
                return _Locator()

        class _Context:
            def storage_state(self, path: str) -> None:
                Path(path).write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            state_path.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
            session_id = "session-auth-check"
            cloud_api.upsert_auth_session(
                session_id,
                {
                    "status": "waiting_credentials",
                    "login_url": "https://www.weiq.com/",
                    "message": "等待登录",
                    "expires_at": cloud_api.expiry_iso(),
                },
            )
            cloud_api.register_active_session(
                session_id,
                task_id="task-auth-check",
                page=_Page(),
                context=_Context(),
                state_storage=str(state_path),
                login_url="https://www.weiq.com/",
            )

            try:
                with patch("cloud_api.infer_runtime_auth_requirement", return_value=(False, cloud_api.ErrorCode.NONE)):
                    row = cloud_api.inspect_active_auth_session(session_id)
            finally:
                cloud_api.unregister_active_session(session_id)

        assert row is not None
        self.assertEqual(row["status"], "waiting_credentials")
        self.assertIn("本机 Chrome 登录不会同步到服务器", row["message"])

    def test_build_status_payload_exposes_queue_and_login_state(self):
        cloud_api.upsert_auth_session(
            "sess-meta-1",
            {
                "status": "waiting_credentials",
                "login_url": "https://www.weiq.com/",
                "message": "等待登录",
                "expires_at": cloud_api.expiry_iso(),
            },
        )
        payload = cloud_api.build_status_payload(
            {
                "task_id": "task-meta-1",
                "status": cloud_api.TaskStatus.PENDING,
                "error_code": cloud_api.ErrorCode.NONE,
                "message": "任务已受理，等待执行器接单",
                "login_session_id": "sess-meta-1",
                "accepted_at": cloud_api.now_iso(),
            }
        )

        self.assertEqual(payload["auth_session_status"], "waiting_credentials")
        self.assertTrue(payload["needs_login"])
        self.assertIn("worker_alive", payload)
        self.assertIn("queue_size", payload)

    def test_build_status_payload_prefers_runtime_message_while_running(self):
        payload = cloud_api.build_status_payload(
            {
                "task_id": "task-1",
                "status": cloud_api.TaskStatus.RUNNING,
                "error_code": cloud_api.ErrorCode.NONE,
                "message": "正在抓取 中华小鸣仔（第 2/4 个）",
            }
        )

        self.assertEqual(payload["error_message_zh"], "正在抓取 中华小鸣仔（第 2/4 个）")

    def test_create_task_with_accounts_json_materializes_runtime_files(self):
        payload = cloud_api.CreateTaskRequest(
            accounts=[cloud_api.AccountInput(nickname="测试账号", uid="1234567890")],
            headless=True,
        )

        response = cloud_api.create_task(payload)
        self.assertTrue(response.task_id)
        row = cloud_api.fetch_one("SELECT * FROM tasks WHERE task_id = ?", (response.task_id,))
        assert row is not None
        self.assertEqual(row["status"], cloud_api.TaskStatus.PENDING)
        self.assertTrue(Path(row["input_excel"]).exists())
        self.assertTrue(Path(row["output_dir"]).exists())

    def test_enqueue_task_rejects_duplicate_active_task(self):
        with cloud_api.QUEUE_LOCK:
            cloud_api.ACTIVE_TASK_IDS.add("task-active-1")
        try:
            self.assertFalse(cloud_api.enqueue_task("task-active-1"))
        finally:
            with cloud_api.QUEUE_LOCK:
                cloud_api.ACTIVE_TASK_IDS.discard("task-active-1")

    def test_run_task_marks_failed_when_runtime_raises(self):
        with patch("cloud_api.enqueue_task", return_value=True):
            task = cloud_api.create_task(
                cloud_api.CreateTaskRequest(
                    accounts=[cloud_api.AccountInput(nickname="测试账号", uid="1234567890")],
                )
            )

        session_id = "sess-failed-1"
        state_storage = cloud_api.build_auth_session_paths(session_id)["state_storage"]
        Path(state_storage).write_text(
            json.dumps({"cookies": [{"name": "sid"}], "origins": []}),
            encoding="utf-8",
        )
        cloud_api.upsert_auth_session(
            session_id,
            {
                "task_id": task.task_id,
                "status": "authenticated",
                "state_storage": state_storage,
                "message": "ok",
                "expires_at": cloud_api.expiry_iso(),
            },
        )
        cloud_api.upsert_task_event(task.task_id, {"login_session_id": session_id})

        with patch("cloud_api.run_crawl", side_effect=RuntimeError("boom")):
            cloud_api.run_task(task.task_id)

        row = cloud_api.fetch_one("SELECT * FROM tasks WHERE task_id = ?", (task.task_id,))
        assert row is not None
        self.assertEqual(row["status"], cloud_api.TaskStatus.FAILED)
        self.assertIn("任务异常崩溃", row["message"])

    def test_run_task_browser_worker_passes_display_to_run_crawl(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            state_path.write_text(json.dumps({"cookies": [{"name": "sid"}], "origins": []}), encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "WEIQ_BROWSER_AUTH_MODE": "browser_worker",
                    "WEIQ_LEGACY_STATE_JSON": str(state_path),
                    "WEIQ_BROWSER_HEADLESS": "false",
                },
                clear=False,
            ), patch("cloud_api.enqueue_task", return_value=True):
                task = cloud_api.create_task(
                    cloud_api.CreateTaskRequest(
                        accounts=[cloud_api.AccountInput(nickname="测试账号", uid="1234567890")],
                    )
                )

            fake_result = cloud_api.CrawlRunResult(
                run_id="run-browser-worker",
                status=cloud_api.TaskStatus.SUCCESS,
                total_accounts=1,
                processed_accounts=1,
                success_accounts=1,
                failed_accounts=0,
                skipped_accounts=0,
                started_at=cloud_api.now_iso(),
                finished_at=cloud_api.now_iso(),
                output_excel="result.xlsx",
                error_code=cloud_api.ErrorCode.NONE,
            )
            with patch.dict(
                os.environ,
                {
                    "WEIQ_BROWSER_AUTH_MODE": "browser_worker",
                    "WEIQ_LEGACY_STATE_JSON": str(state_path),
                    "WEIQ_BROWSER_HEADLESS": "false",
                },
                clear=False,
            ), patch("cloud_api.ensure_browser_display", return_value=":99"), patch(
                "cloud_api.run_crawl",
                return_value=fake_result,
            ) as run_crawl_mock:
                cloud_api.run_task(task.task_id)

        config = run_crawl_mock.call_args.kwargs["config"]
        self.assertEqual(config.display, ":99")

    def test_run_task_browser_worker_fails_cleanly_when_display_unavailable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            state_path.write_text(json.dumps({"cookies": [{"name": "sid"}], "origins": []}), encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "WEIQ_BROWSER_AUTH_MODE": "browser_worker",
                    "WEIQ_LEGACY_STATE_JSON": str(state_path),
                    "WEIQ_BROWSER_HEADLESS": "false",
                },
                clear=False,
            ), patch("cloud_api.enqueue_task", return_value=True):
                task = cloud_api.create_task(
                    cloud_api.CreateTaskRequest(
                        accounts=[cloud_api.AccountInput(nickname="测试账号", uid="1234567890")],
                    )
                )

            with patch.dict(
                os.environ,
                {
                    "WEIQ_BROWSER_AUTH_MODE": "browser_worker",
                    "WEIQ_LEGACY_STATE_JSON": str(state_path),
                    "WEIQ_BROWSER_HEADLESS": "false",
                },
                clear=False,
            ), patch(
                "cloud_api.ensure_browser_display",
                side_effect=RuntimeError(cloud_api.BROWSER_DISPLAY_UNAVAILABLE_MESSAGE),
            ), patch("cloud_api.run_crawl") as run_crawl_mock:
                cloud_api.run_task(task.task_id)

        run_crawl_mock.assert_not_called()
        row = cloud_api.fetch_one("SELECT status, error_code, message FROM tasks WHERE task_id = ?", (task.task_id,))
        assert row is not None
        self.assertEqual(row["status"], cloud_api.TaskStatus.FAILED)
        self.assertEqual(row["error_code"], "DISPLAY_UNAVAILABLE")
        self.assertEqual(row["message"], cloud_api.BROWSER_DISPLAY_UNAVAILABLE_MESSAGE)

    def test_run_task_blocks_when_task_has_no_authenticated_session(self):
        with patch("cloud_api.enqueue_task", return_value=True):
            task = cloud_api.create_task(
                cloud_api.CreateTaskRequest(
                    accounts=[cloud_api.AccountInput(nickname="测试账号", uid="1234567890")],
                )
            )

        eager_row = {
            "session_id": "sess-blocked-1",
            "task_id": task.task_id,
            "status": "waiting_credentials",
            "login_url": "https://www.weiq.com/",
            "state_storage": cloud_api.build_auth_session_paths("sess-blocked-1")["state_storage"],
            "message": "等待登录",
            "expires_at": cloud_api.expiry_iso(),
        }

        with patch("cloud_api.uuid4") as uuid_mock, patch(
            "cloud_api._build_eager_auth_session_state",
            return_value=eager_row,
        ), patch("cloud_api.run_crawl") as run_crawl_mock:
            uuid_mock.return_value.hex = "sess-blocked-1"
            cloud_api.run_task(task.task_id)

        run_crawl_mock.assert_not_called()
        row = cloud_api.fetch_one("SELECT status, login_session_id, blocked_reason FROM tasks WHERE task_id = ?", (task.task_id,))
        assert row is not None
        self.assertEqual(row["status"], cloud_api.TaskStatus.BLOCKED_AUTH)
        self.assertTrue(str(row["login_session_id"]))
        self.assertEqual(row["blocked_reason"], cloud_api.ErrorCode.AUTH_REQUIRED)

    def test_get_task_exposes_blocked_auth_context(self):
        with patch("cloud_api.enqueue_task", return_value=True):
            task = cloud_api.create_task(
                cloud_api.CreateTaskRequest(
                    accounts=[cloud_api.AccountInput(nickname="测试账号", uid="1234567890")],
                )
            )
        cloud_api.upsert_task_event(
            task.task_id,
            {
                "status": cloud_api.TaskStatus.BLOCKED_AUTH,
                "blocked_reason": cloud_api.ErrorCode.CAPTCHA_REQUIRED,
                "error_code": "BLOCKED_AUTH",
                "message": "WEIQ 要求安全验证，请在远端浏览器中完成验证后继续。",
                "current_url": "https://www.weiq.com/security",
                "page_title": "安全验证",
                "screenshot_path": "/tmp/blocked-auth.png",
                "can_resume": 1,
            },
        )

        with TestClient(cloud_api.app) as client:
            resp = client.get(f"/v1/tasks/{task.task_id}")

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], cloud_api.TaskStatus.BLOCKED_AUTH)
        self.assertEqual(body["current_url"], "https://www.weiq.com/security")
        self.assertEqual(body["page_title"], "安全验证")
        self.assertEqual(body["screenshot_path"], "/tmp/blocked-auth.png")
        self.assertTrue(body["can_resume"])

    def test_resume_after_auth_returns_blocked_auth_when_verification_not_finished(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            state_path.write_text(json.dumps({"cookies": [{"name": "sid"}], "origins": []}), encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "WEIQ_BROWSER_AUTH_MODE": "browser_worker",
                    "WEIQ_LEGACY_STATE_JSON": str(state_path),
                    "WEIQ_BROWSER_HEADLESS": "false",
                },
                clear=False,
            ), patch("cloud_api.enqueue_task", return_value=True):
                task = cloud_api.create_task(
                    cloud_api.CreateTaskRequest(
                        accounts=[cloud_api.AccountInput(nickname="测试账号", uid="1234567890")],
                    )
                )
            cloud_api.upsert_task_event(task.task_id, {"status": cloud_api.TaskStatus.BLOCKED_AUTH, "can_resume": 1})
            fake_page = _AsyncFakeBlockedPage()
            fake_context = _AsyncFakeContext()
            fake_context.pages = [fake_page]
            cloud_api.BROWSER_WORKER_CONTROLLER._context = fake_context
            cloud_api.BROWSER_WORKER_CONTROLLER._page = fake_page
            cloud_api.BROWSER_WORKER_CONTROLLER._playwright = _AsyncFakePlaywright()
            cloud_api.BROWSER_WORKER_CONTROLLER._state = {
                "session_id": "session-blocked",
                "runtime_state": "open",
                "browser_session_running": True,
                "live_auth_verified": False,
            }

            with patch.dict(
                os.environ,
                {
                    "WEIQ_BROWSER_AUTH_MODE": "browser_worker",
                    "WEIQ_LEGACY_STATE_JSON": str(state_path),
                    "WEIQ_BROWSER_HEADLESS": "false",
                },
                clear=False,
            ):
                with TestClient(cloud_api.app) as client:
                    resp = client.post(f"/v1/tasks/{task.task_id}/resume-after-auth")

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], cloud_api.TaskStatus.BLOCKED_AUTH)
        self.assertEqual(body["current_url"], "https://www.weiq.com/security")
        self.assertEqual(body["page_title"], "安全验证")
        self.assertTrue(body["can_resume"])

    def test_resume_after_auth_requeues_task_after_verification(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            state_path.write_text(json.dumps({"cookies": [{"name": "sid"}], "origins": []}), encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "WEIQ_BROWSER_AUTH_MODE": "browser_worker",
                    "WEIQ_LEGACY_STATE_JSON": str(state_path),
                    "WEIQ_BROWSER_HEADLESS": "false",
                },
                clear=False,
            ), patch("cloud_api.enqueue_task", return_value=True):
                task = cloud_api.create_task(
                    cloud_api.CreateTaskRequest(
                        accounts=[cloud_api.AccountInput(nickname="测试账号", uid="1234567890")],
                    )
                )
            cloud_api.upsert_task_event(task.task_id, {"status": cloud_api.TaskStatus.BLOCKED_AUTH, "run_id": "run-existing-1", "can_resume": 1})
            fake_page = _AsyncFakePage()
            fake_context = _AsyncFakeContext()
            fake_context.pages = [fake_page]
            cloud_api.BROWSER_WORKER_CONTROLLER._context = fake_context
            cloud_api.BROWSER_WORKER_CONTROLLER._page = fake_page
            cloud_api.BROWSER_WORKER_CONTROLLER._playwright = _AsyncFakePlaywright()
            cloud_api.BROWSER_WORKER_CONTROLLER._state = {
                "session_id": "session-verified",
                "runtime_state": "open",
                "browser_session_running": True,
                "live_auth_verified": False,
            }

            with patch.dict(
                os.environ,
                {
                    "WEIQ_BROWSER_AUTH_MODE": "browser_worker",
                    "WEIQ_LEGACY_STATE_JSON": str(state_path),
                    "WEIQ_BROWSER_HEADLESS": "false",
                },
                clear=False,
            ), patch("cloud_api.has_usable_storage_state", return_value=True), patch(
                "cloud_api.enqueue_task",
                return_value=True,
            ):
                with TestClient(cloud_api.app) as client:
                    resp = client.post(f"/v1/tasks/{task.task_id}/resume-after-auth")

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], cloud_api.TaskStatus.PENDING)
        row = cloud_api.fetch_one("SELECT status, run_id FROM tasks WHERE task_id = ?", (task.task_id,))
        assert row is not None
        self.assertEqual(row["status"], cloud_api.TaskStatus.PENDING)
        self.assertEqual(row["run_id"], "run-existing-1")

    def test_run_task_reuses_existing_run_id_for_resume(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            state_path.write_text(json.dumps({"cookies": [{"name": "sid"}], "origins": []}), encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "WEIQ_BROWSER_AUTH_MODE": "browser_worker",
                    "WEIQ_LEGACY_STATE_JSON": str(state_path),
                    "WEIQ_BROWSER_HEADLESS": "false",
                },
                clear=False,
            ), patch("cloud_api.enqueue_task", return_value=True):
                task = cloud_api.create_task(
                    cloud_api.CreateTaskRequest(
                        accounts=[cloud_api.AccountInput(nickname="测试账号", uid="1234567890")],
                    )
                )
            cloud_api.upsert_task_event(task.task_id, {"run_id": "run-existing-1"})

            fake_result = cloud_api.CrawlRunResult(
                run_id="run-existing-1",
                status=cloud_api.TaskStatus.SUCCESS,
                total_accounts=1,
                processed_accounts=1,
                success_accounts=1,
                failed_accounts=0,
                skipped_accounts=0,
                started_at=cloud_api.now_iso(),
                finished_at=cloud_api.now_iso(),
                output_excel="result.xlsx",
                error_code=cloud_api.ErrorCode.NONE,
            )
            with patch.dict(
                os.environ,
                {
                    "WEIQ_BROWSER_AUTH_MODE": "browser_worker",
                    "WEIQ_LEGACY_STATE_JSON": str(state_path),
                    "WEIQ_BROWSER_HEADLESS": "false",
                },
                clear=False,
            ), patch("cloud_api.ensure_browser_display", return_value=":99"), patch(
                "cloud_api.run_crawl",
                return_value=fake_result,
            ) as run_crawl_mock:
                cloud_api.run_task(task.task_id)

        config = run_crawl_mock.call_args.kwargs["config"]
        self.assertEqual(config.run_id, "run-existing-1")

    def test_recover_incomplete_tasks_requeues_pending_and_running(self):
        cloud_api.execute(
            """
            INSERT INTO tasks (
                task_id, status, progress, error_code, message, input_excel, output_excel, output_dir,
                state_json, state_storage, headless, cooldown_every, cooldown_seconds, retry_times,
                retry_backoff_seconds, resume, created_at
            ) VALUES (?, ?, 0, ?, ?, ?, ?, ?, ?, ?, 1, 50, 180, 1, 3, 1, ?)
            """,
            (
                "pending-1",
                cloud_api.TaskStatus.PENDING,
                cloud_api.ErrorCode.NONE,
                "任务已创建",
                "a.xlsx",
                "b.xlsx",
                ".",
                "state.json",
                "crawl_state.json",
                cloud_api.now_iso(),
            ),
        )
        cloud_api.execute(
            """
            INSERT INTO tasks (
                task_id, status, progress, error_code, message, input_excel, output_excel, output_dir,
                state_json, state_storage, headless, cooldown_every, cooldown_seconds, retry_times,
                retry_backoff_seconds, resume, created_at
            ) VALUES (?, ?, 0.3, ?, ?, ?, ?, ?, ?, ?, 1, 50, 180, 1, 3, 1, ?)
            """,
            (
                "running-1",
                cloud_api.TaskStatus.RUNNING,
                cloud_api.ErrorCode.NONE,
                "任务运行中",
                "a.xlsx",
                "b.xlsx",
                ".",
                "state.json",
                "crawl_state.json",
                cloud_api.now_iso(),
            ),
        )

        cloud_api.recover_incomplete_tasks()

        with cloud_api.QUEUE_LOCK:
            tracked_ids = set(cloud_api.QUEUED_TASK_IDS) | set(cloud_api.ACTIVE_TASK_IDS)
            self.assertIn("pending-1", tracked_ids)
            self.assertIn("running-1", tracked_ids)
        row = cloud_api.fetch_one("SELECT status, message FROM tasks WHERE task_id = ?", ("running-1",))
        assert row is not None
        self.assertIn(row["status"], {cloud_api.TaskStatus.PENDING, cloud_api.TaskStatus.RUNNING})
        self.assertTrue("重新入队" in row["message"] or "已接单" in row["message"])

    def test_worker_health_endpoint_returns_runtime_state(self):
        with TestClient(cloud_api.app) as client:
            resp = client.get("/v1/worker/health")
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertIn("worker_alive", body)
            self.assertIn("queue_size", body)
            self.assertIn("db_path", body)

    def test_requeue_endpoint_resets_task_to_pending(self):
        with patch("cloud_api.enqueue_task", return_value=True):
            task = cloud_api.create_task(
                cloud_api.CreateTaskRequest(
                    accounts=[cloud_api.AccountInput(nickname="测试账号", uid="1234567890")],
                )
            )
        cloud_api.upsert_task_event(
            task.task_id,
            {
                "status": cloud_api.TaskStatus.FAILED,
                "progress": 1.0,
                "message": "任务失败",
                "finished_at": cloud_api.now_iso(),
                "cancel_requested": 1,
            },
        )

        with TestClient(cloud_api.app) as client:
            with patch("cloud_api.enqueue_task", return_value=True):
                resp = client.post(f"/v1/tasks/{task.task_id}/requeue")
            self.assertEqual(resp.status_code, 200)
        row = cloud_api.fetch_one(
            "SELECT status, progress, cancel_requested, message FROM tasks WHERE task_id = ?",
            (task.task_id,),
        )
        assert row is not None
        self.assertIn(row["status"], {cloud_api.TaskStatus.PENDING, cloud_api.TaskStatus.RUNNING})
        self.assertIn(row["progress"], {0.0, 1.0})
        self.assertEqual(row["cancel_requested"], 0)
        self.assertTrue("重新入队" in row["message"] or "已接单" in row["message"])

    def test_export_endpoint_requires_success_and_returns_file(self):
        with TestClient(cloud_api.app) as client, patch("cloud_api.enqueue_task", return_value=True):
            task = cloud_api.create_task(
                cloud_api.CreateTaskRequest(
                    accounts=[cloud_api.AccountInput(nickname="测试账号", uid="1234567890")],
                )
            )
            not_ready = client.get(f"/v1/tasks/{task.task_id}/export")
            self.assertEqual(not_ready.status_code, 409)

            row = cloud_api.fetch_one("SELECT output_excel, output_dir FROM tasks WHERE task_id = ?", (task.task_id,))
            assert row is not None
            output_path = Path(row["output_dir"]) / row["output_excel"]
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"dummy")
            cloud_api.upsert_task_event(task.task_id, {"status": cloud_api.TaskStatus.SUCCESS, "message": "任务已完成"})

            ready = client.get(f"/v1/tasks/{task.task_id}/export")
            self.assertEqual(ready.status_code, 200)
            self.assertEqual(ready.content, b"dummy")


if __name__ == "__main__":
    unittest.main()
