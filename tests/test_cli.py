import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from support import jsonl


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.state = self.home / "state"
        self.env = os.environ.copy()
        self.env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "agent_workflow_kit", "--state", str(self.state), *args],
            env=self.env, capture_output=True, text=True, timeout=20,
        )

    def test_help_lists_all_three_tools(self):
        result = self.run_cli("--help")
        self.assertEqual(result.returncode, 0)
        for name in ("history", "verify", "fleet"):
            self.assertIn(name, result.stdout)

    def test_history_index_search_recover_end_to_end(self):
        path = self.home / ".factory/sessions/project/synthetic.jsonl"
        jsonl(path, [
            {"type": "session_start", "id": "synthetic", "cwd": "/example"},
            {"type": "message", "message": {"role": "user", "content": [
                {"type": "text", "text": "Fix browser regression"}]}},
            {"type": "message", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "Next: verify the browser"}]}},
        ])
        result = self.run_cli("history", "index", "--home", str(self.home),
                              "--sources", "droid", "--since-days", "0")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(json.loads(result.stdout)["droid"]["updated"], 1)
        result = self.run_cli("history", "search", "browser", "--project", "example")
        self.assertEqual(len(json.loads(result.stdout)), 2)
        result = self.run_cli("history", "recover", "droid", "synthetic")
        self.assertEqual(json.loads(result.stdout)["session_id"], "synthetic")

    def test_stats_and_empty_fleet_are_valid_json(self):
        for args in [("history", "stats"), ("fleet", "status")]:
            result = self.run_cli(*args)
            self.assertEqual(result.returncode, 0)
            json.loads(result.stdout)

    def test_invalid_operations_fail_cleanly(self):
        result = self.run_cli("history", "recover", "claude", "missing")
        self.assertEqual(result.returncode, 1)
        self.assertIn("error", json.loads(result.stdout))
        result = self.run_cli("history", "search", "***")
        self.assertEqual(result.returncode, 1)
        json.loads(result.stdout)

    def test_bounded_watch_exits(self):
        result = self.run_cli("history", "watch", "--home", str(self.home),
                              "--sources", "droid", "--iterations", "1", "--interval", "0.1")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["droid"]["examined"], 0)

    def test_invalid_watch_interval_is_rejected(self):
        result = self.run_cli("history", "watch", "--interval", "0")
        self.assertEqual(result.returncode, 1)
        self.assertIn("error", json.loads(result.stdout))

    def test_malformed_plan_is_an_error(self):
        plan = self.home / "bad.json"
        plan.write_text("{")
        result = self.run_cli("fleet", "plan", str(plan))
        self.assertEqual(result.returncode, 1)
        self.assertIn("error", json.loads(result.stdout))


if __name__ == "__main__":
    unittest.main()
