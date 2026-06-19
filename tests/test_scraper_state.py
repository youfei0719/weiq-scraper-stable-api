import tempfile
import unittest
from pathlib import Path

from scraper import StateStore


class TestStateStore(unittest.TestCase):
    def test_mark_and_resume_state(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_path = Path(tmp_dir) / "state_store.json"
            store = StateStore(str(state_path))

            run_id = "run_test_001"
            store.ensure_run(run_id)
            store.mark_processed(run_id, "uid_1", "SUCCESS", "NONE")

            store2 = StateStore(str(state_path))
            self.assertEqual(store2.get_last_run_id(), run_id)
            self.assertTrue(store2.is_processed(run_id, "uid_1"))
            self.assertFalse(store2.is_processed(run_id, "uid_2"))


if __name__ == "__main__":
    unittest.main()
