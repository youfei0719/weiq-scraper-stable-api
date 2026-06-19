import unittest

import pandas as pd

from analytics import incremental_changes, quality_report


class TestAnalytics(unittest.TestCase):
    def test_incremental_changes(self):
        df = pd.DataFrame(
            [
                {"uid": "u1", "crawl_time": "2026-01-01T10:00:00", "粉丝数": "100", "直发CPM": "10"},
                {"uid": "u1", "crawl_time": "2026-01-02T10:00:00", "粉丝数": "120", "直发CPM": "10"},
                {"uid": "u1", "crawl_time": "2026-01-03T10:00:00", "粉丝数": "120", "直发CPM": "12"},
            ]
        )
        result = incremental_changes(df, uid="u1")
        self.assertEqual(result["count"], 2)

    def test_quality_report(self):
        df = pd.DataFrame(
            [
                {"account_status": "SUCCESS", "粉丝数": "100", "直发CPM": "10", "阅读中位数": "200", "发布博文数": "3"},
                {"account_status": "FAILED", "粉丝数": "空", "直发CPM": "空", "阅读中位数": "空", "发布博文数": "空"},
            ]
        )
        report = quality_report(df)
        self.assertIn("score", report)
        self.assertGreaterEqual(report["score"], 0)
        self.assertLessEqual(report["score"], 100)


if __name__ == "__main__":
    unittest.main()
