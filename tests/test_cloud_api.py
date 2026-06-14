import os
import sys
import time
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import cloud_api


class TestCloudAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.path.exists(cloud_api.DB_PATH):
            os.remove(cloud_api.DB_PATH)

    def setUp(self):
        while not cloud_api.TASK_QUEUE.empty():
            try:
                cloud_api.TASK_QUEUE.get_nowait()
                cloud_api.TASK_QUEUE.task_done()
            except Exception:
                break
        with cloud_api.QUEUE_LOCK:
            cloud_api.QUEUED_TASK_IDS.clear()
        cloud_api.init_db()

    def test_health(self):
        with TestClient(cloud_api.app) as client:
            resp = client.get("/health")
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["status"], "ok")

    def test_task_lifecycle_minimal(self):
        with patch("cloud_api.run_crawl", side_effect=RuntimeError("boom")):
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
                cloud_api.run_task(task_id)
                task_resp = client.get(f"/v1/tasks/{task_id}")
                self.assertEqual(task_resp.status_code, 200)
                self.assertIn(task_resp.json()["status"], {"FAILED", "SUCCESS", "CANCELLED"})

                cancel_resp = client.post(f"/v1/tasks/{task_id}/cancel")
                self.assertEqual(cancel_resp.status_code, 200)

    def test_auth_session_endpoints_minimal(self):
        with TestClient(cloud_api.app) as client:
            create_resp = client.post("/v1/auth/session")
            self.assertEqual(create_resp.status_code, 200)
            session = create_resp.json()
            self.assertEqual(session["status"], "pending")
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

    def test_create_auth_session_eager_binds_task_and_returns_waiting_state(self):
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
        ):
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
                state_json=str(state_path),
                login_url="https://www.weiq.com/",
            )

            try:
                with patch("cloud_api.infer_runtime_auth_requirement", return_value=(False, cloud_api.ErrorCode.NONE)):
                    row = cloud_api.inspect_active_auth_session(session_id)
            finally:
                cloud_api.unregister_active_session(session_id)

        assert row is not None
        self.assertEqual(row["status"], "waiting_credentials")
        self.assertIn("尚未检测到有效的 WEIQ 登录态", row["message"])

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

    def test_run_task_marks_failed_when_runtime_raises(self):
        task = cloud_api.create_task(
            cloud_api.CreateTaskRequest(
                accounts=[cloud_api.AccountInput(nickname="测试账号", uid="1234567890")],
            )
        )

        with patch("cloud_api.run_crawl", side_effect=RuntimeError("boom")):
            cloud_api.run_task(task.task_id)

        row = cloud_api.fetch_one("SELECT * FROM tasks WHERE task_id = ?", (task.task_id,))
        assert row is not None
        self.assertEqual(row["status"], cloud_api.TaskStatus.FAILED)
        self.assertIn("任务异常崩溃", row["message"])

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
            self.assertIn("pending-1", cloud_api.QUEUED_TASK_IDS)
            self.assertIn("running-1", cloud_api.QUEUED_TASK_IDS)
        row = cloud_api.fetch_one("SELECT status, message FROM tasks WHERE task_id = ?", ("running-1",))
        assert row is not None
        self.assertEqual(row["status"], cloud_api.TaskStatus.PENDING)
        self.assertIn("重新入队", row["message"])

    def test_worker_health_endpoint_returns_runtime_state(self):
        with TestClient(cloud_api.app) as client:
            resp = client.get("/v1/worker/health")
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertIn("worker_alive", body)
            self.assertIn("queue_size", body)
            self.assertIn("db_path", body)

    def test_requeue_endpoint_resets_task_to_pending(self):
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
            resp = client.post(f"/v1/tasks/{task.task_id}/requeue")
            self.assertEqual(resp.status_code, 200)
        row = cloud_api.fetch_one(
            "SELECT status, progress, cancel_requested, message FROM tasks WHERE task_id = ?",
            (task.task_id,),
        )
        assert row is not None
        self.assertEqual(row["status"], cloud_api.TaskStatus.PENDING)
        self.assertEqual(row["progress"], 0.0)
        self.assertEqual(row["cancel_requested"], 0)
        self.assertIn("重新入队", row["message"])

    def test_export_endpoint_requires_success_and_returns_file(self):
        with TestClient(cloud_api.app) as client:
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
