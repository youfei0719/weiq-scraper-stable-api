import os
import sys
import time
import unittest
from pathlib import Path

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
        cloud_api.init_db()

    def test_health(self):
        with TestClient(cloud_api.app) as client:
            resp = client.get("/health")
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["status"], "ok")

    def test_task_lifecycle_minimal(self):
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

            seen_terminal = False
            for _ in range(20):
                task_resp = client.get(f"/v1/tasks/{task_id}")
                self.assertEqual(task_resp.status_code, 200)
                status = task_resp.json()["status"]
                if status in {"FAILED", "SUCCESS", "CANCELLED"}:
                    seen_terminal = True
                    break
                time.sleep(0.2)

            self.assertTrue(seen_terminal)

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


if __name__ == "__main__":
    unittest.main()
