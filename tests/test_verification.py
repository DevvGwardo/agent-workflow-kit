import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from support import git, repository
from agent_workflow_kit.common import state_root
from agent_workflow_kit.gitstate import fingerprint
from agent_workflow_kit.verification import Verification, load_gates


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.project = repository(self.home / "repo")
        self.root = state_root(self.home / "state")
        self.verify = Verification(self.root)
        self.gates = self.home / "gates.json"

    def tearDown(self):
        self.tmp.cleanup()

    def config(self, checks=None):
        value = {"version": 1, "checks": checks or [
            {"id": "test", "command": [sys.executable, "-c", "print('PASS')"], "expect": "PASS"}
        ]}
        self.gates.write_text(json.dumps(value))
        return self.gates

    def test_pass_receipt_is_bound_to_commit(self):
        result = self.verify.run(self.project, self.config())
        self.assertTrue(result["passed"])
        self.assertEqual(result["phases"]["implemented"], "passed")
        self.assertEqual(result["phases"]["released"], "not_configured")
        self.assertTrue(self.verify.status(self.project, self.gates)["passed"])
        git(self.project, "commit", "--allow-empty", "-m", "test: change commit",
            "-m", "Co-authored-by: factory-droid[bot] <138933559+factory-droid[bot]@users.noreply.github.com>")
        self.assertEqual(self.verify.status(self.project, self.gates)["state"], "stale")

    def test_dirty_contents_invalidate_even_with_same_git_status(self):
        tracked = self.project / "tracked.txt"
        tracked.write_text("dirty one\n")
        self.verify.run(self.project, self.config())
        tracked.write_text("dirty two\n")
        self.assertEqual(self.verify.status(self.project, self.gates)["state"], "stale")

    def test_untracked_and_staged_changes_invalidate(self):
        self.verify.run(self.project, self.config())
        (self.project / "new.txt").write_text("new\n")
        self.assertFalse(self.verify.status(self.project, self.gates)["passed"])
        git(self.project, "add", "new.txt")
        self.assertFalse(self.verify.status(self.project, self.gates)["passed"])

    def test_config_change_invalidates(self):
        self.verify.run(self.project, self.config())
        self.gates.write_text(self.gates.read_text() + "\n")
        self.assertIn("Gate configuration changed",
                      self.verify.status(self.project, self.gates)["stale_reasons"])

    def test_changed_index_invalidates_with_identical_worktree_and_status(self):
        tracked = self.project / "tracked.txt"
        tracked.write_text("staged-one")
        git(self.project, "add", "tracked.txt")
        tracked.write_text("working-copy")
        self.verify.run(self.project, self.config())
        before = git(self.project, "status", "--porcelain")
        tracked.write_text("staged-two")
        git(self.project, "add", "tracked.txt")
        tracked.write_text("working-copy")
        self.assertEqual(before, git(self.project, "status", "--porcelain"))
        self.assertEqual(self.verify.status(self.project, self.gates)["state"], "stale")

    def test_nonzero_exit_cannot_pass_expectation(self):
        self.config([{"id": "bad", "command": [sys.executable, "-c", "print('PASS'); raise SystemExit(1)"],
                      "expect": "PASS"}])
        receipt = self.verify.run(self.project, self.gates)
        self.assertFalse(receipt["passed"])
        self.assertEqual(receipt["checks"][0]["state"], "failed")

    def test_missing_expectation_fails(self):
        self.config([{"id": "bad", "command": [sys.executable, "-c", "print('OTHER')"], "expect": "PASS"}])
        self.assertFalse(self.verify.run(self.project, self.gates)["passed"])

    def test_timeout_is_failure(self):
        self.config([{"id": "timeout", "command": [sys.executable, "-c", "import time; time.sleep(5)"],
                      "timeout_seconds": 0.1}])
        receipt = self.verify.run(self.project, self.gates)
        self.assertFalse(receipt["passed"])
        self.assertEqual(receipt["checks"][0]["state"], "timed_out")

    def test_missing_executable_is_failure(self):
        self.config([{"id": "missing", "command": ["nonexistent-executable-fixture"]}])
        self.assertFalse(self.verify.run(self.project, self.gates)["passed"])

    def test_optional_failure_is_explicit(self):
        self.config([
            {"id": "required", "command": [sys.executable, "-c", "print('ok')"]},
            {"id": "optional", "required": False, "command": [sys.executable, "-c", "raise SystemExit(1)"]},
        ])
        receipt = self.verify.run(self.project, self.gates)
        self.assertTrue(receipt["passed"])
        self.assertEqual(receipt["phases"]["implemented"], "passed_with_optional_failures")

    def test_state_change_stops_remaining_gates(self):
        self.config([
            {"id": "mutate", "command": [sys.executable, "-c",
                "from pathlib import Path; Path('tracked.txt').write_text('changed')"]},
            {"id": "later", "command": [sys.executable, "-c", "print('must not run')"]},
        ])
        receipt = self.verify.run(self.project, self.gates)
        self.assertFalse(receipt["passed"])
        self.assertEqual(receipt["checks"][1]["state"], "skipped")

    def test_artifacts_are_required_and_rechecked(self):
        artifact = self.project / "evidence/smoke.txt"
        artifact.parent.mkdir()
        (self.project / ".gitignore").write_text("evidence/\n")
        self.config([{"id": "smoke", "phase": "integrated",
                      "command": [sys.executable, "-c", "print('ok')"], "artifacts": ["evidence/smoke.txt"]}])
        self.assertFalse(self.verify.run(self.project, self.gates)["passed"])
        artifact.write_text("real synthetic evidence\n")
        self.assertTrue(self.verify.run(self.project, self.gates)["passed"])
        artifact.write_text("different evidence\n")
        status = self.verify.status(self.project, self.gates)
        self.assertIn("Evidence artifact changed or disappeared", status["stale_reasons"])

    def test_redacts_output_and_bounds_evidence(self):
        self.config([{"id": "output", "command": [sys.executable, "-c",
            "print('x'*10000); print('API_KEY=' + 'synthetic-value')"]}])
        receipt = self.verify.run(self.project, self.gates)
        output = receipt["checks"][0]["output_tail"]
        self.assertNotIn("synthetic-value", output)
        self.assertTrue(receipt["checks"][0]["output_truncated"])
        self.assertLess(len(output), 4100)
        self.assertNotIn("synthetic-value", Path(receipt["receipt_path"]).read_text())

    def test_old_receipt_is_stale(self):
        self.verify.run(self.project, self.config())
        self.assertEqual(self.verify.status(self.project, self.gates, 0)["state"], "stale")

    def test_missing_receipt_does_not_claim_success(self):
        self.assertEqual(self.verify.status(self.project, self.config())["state"], "missing")

    def test_invalid_gate_configuration_is_rejected_before_execution(self):
        for check in [
            {"id": "shell", "command": ["bash", "-c", "echo ok"]},
            {"id": "bad", "command": "echo ok"},
            {"id": "bad", "command": ["echo"], "cwd": ".."},
            {"id": "bad", "command": ["echo"], "artifacts": ["../outside"]},
            {"id": "bad", "command": ["echo"], "expect": "["},
            {"id": "bad", "command": ["echo"], "timeout_seconds": 0},
        ]:
            with self.subTest(check=check):
                self.config([check])
                with self.assertRaises(ValueError):
                    load_gates(self.gates, self.project)

    def test_symlink_outside_project_is_rejected(self):
        if os.name != "posix":
            self.skipTest("POSIX symlinks")
        (self.project / "escape").symlink_to(self.home, target_is_directory=True)
        self.config([{"id": "bad", "command": ["echo"], "cwd": "escape"}])
        with self.assertRaises(ValueError):
            load_gates(self.gates, self.project)

    def test_fingerprint_does_not_read_symlink_target(self):
        target = self.home / "outside"
        target.write_text("outside-one")
        link = self.project / "link"
        link.symlink_to(target)
        before = fingerprint(self.project)
        target.write_text("outside-two")
        self.assertEqual(before, fingerprint(self.project))


if __name__ == "__main__":
    unittest.main()
