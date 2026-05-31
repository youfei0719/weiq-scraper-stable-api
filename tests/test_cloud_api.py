import os
import time
import unittest

from fastapi.testclient import TestClient

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


if __name__ == "__main__":
    unittest.main()
