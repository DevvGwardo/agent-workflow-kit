import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from support import git, repository
from agent_workflow_kit.common import state_root
from agent_workflow_kit.fleet import Fleet, normalize_claim, overlaps, worktrees
from agent_workflow_kit.verification import Verification


class FleetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.repo = repository(self.home / "repo")
        self.root = state_root(self.home / "state")
        self.fleet = Fleet(self.root)

    def tearDown(self):
        self.fleet.close()
        self.tmp.cleanup()

    def plan(self, workers=None, repo=None, **kwargs):
        value = {"version": 1, "repo": str(repo or self.repo), "workers": workers or [
            {"name": "ui", "files": ["tracked.txt"], "task": "Improve synthetic UI"}
        ], **kwargs}
        return self.fleet.plan(value, self.home)

    def workspace(self, name="worker"):
        path = self.home / name
        git(self.repo, "worktree", "add", "-b", name, str(path))
        return path

    def receipt(self, workspace):
        gates = self.home / "gates.json"
        gates.write_text(json.dumps({"version": 1, "checks": [
            {"id": "test", "command": [sys.executable, "-c", "print('pass')"]}
        ]}))
        return Verification(self.root).run(workspace, gates)

    def test_plan_does_not_launch_and_has_durable_brief(self):
        with patch("agent_workflow_kit.fleet.subprocess.run", wraps=__import__("subprocess").run) as run:
            planned = self.plan()[0]
            self.assertFalse(any(call.args[0][0] in ("warroom", "tmux-orch") for call in run.call_args_list))
        self.assertTrue(Path(planned["brief_path"]).is_file())
        recovered = self.fleet.recover(planned["id"])
        self.assertIn("Improve synthetic UI", recovered["brief"])
        self.assertIn("Do not merge, push", recovered["brief"])

    def test_overlapping_plan_is_atomic(self):
        with self.assertRaisesRegex(ValueError, "Ownership conflict"):
            self.plan([
                {"name": "one", "files": ["src/"], "task": "one"},
                {"name": "two", "files": ["src/file.ts"], "task": "two"},
            ])
        self.assertEqual(self.fleet.status(), [])

    def test_active_claims_conflict_across_worktrees(self):
        self.plan()
        other = self.workspace()
        with self.assertRaisesRegex(ValueError, "Ownership conflict"):
            self.plan(repo=other)

    def test_disjoint_workers_are_allowed(self):
        planned = self.plan([
            {"name": "ui", "files": ["src/ui.ts"], "task": "UI"},
            {"name": "api", "files": ["src/api.ts"], "task": "API"},
        ])
        self.assertEqual(len(planned), 2)
        self.assertEqual(len(self.fleet.status()), 2)

    def test_path_escape_glob_and_metadata_claims_rejected(self):
        for claim in ("../outside", "/outside", "*", ".git/config", ".GIT/config", ".", "a,b"):
            with self.subTest(claim=claim), self.assertRaises(ValueError):
                normalize_claim(self.repo, claim)
        (self.repo / "escape").symlink_to(self.home, target_is_directory=True)
        with self.assertRaises(ValueError):
            normalize_claim(self.repo, "escape/outside")

    def test_case_and_directory_overlap_are_conservative(self):
        self.assertTrue(overlaps("SRC", "src/a.ts"))
        self.assertFalse(overlaps("src/foo", "src/foobar"))

    def test_launch_is_preview_by_default(self):
        task = self.plan(kind="droid", model="example-model")[0]
        with patch("agent_workflow_kit.fleet.subprocess.run") as run:
            result = self.fleet.launch(task["id"])
            run.assert_not_called()
        self.assertFalse(result["executed"])
        self.assertIn("--task-file", result["command"])
        self.assertIn("--base", result["command"])
        self.assertIn("example-model", result["command"])
        self.assertEqual(self.fleet.get(task["id"])["state"], "planned")

    def test_explicit_launch_uses_existing_backend(self):
        task = self.plan()[0]
        with patch("agent_workflow_kit.fleet.shutil.which", return_value="/launcher"), \
             patch("agent_workflow_kit.fleet.subprocess.run") as run, \
             patch.object(self.fleet, "discover_worktree", return_value=""):
            run.return_value.returncode = 0
            run.return_value.stdout = "worker started"
            run.return_value.stderr = ""
            result = self.fleet.launch(task["id"], True)
        self.assertEqual(result["state"], "running")
        with self.assertRaises(ValueError):
            self.fleet.launch(task["id"])

    def test_failed_launch_retains_claims(self):
        task = self.plan()[0]
        with patch("agent_workflow_kit.fleet.shutil.which", return_value="/launcher"), \
             patch("agent_workflow_kit.fleet.subprocess.run") as run, \
             patch.object(self.fleet, "discover_worktree", return_value=""):
            run.return_value.returncode = 1
            run.return_value.stdout = ""
            run.return_value.stderr = "launch blocked"
            self.assertEqual(self.fleet.launch(task["id"], True)["state"], "failed")
        with self.assertRaises(ValueError):
            self.plan()

    def test_block_and_stale_heartbeat_are_explicit(self):
        task = self.plan()[0]
        self.fleet.block(task["id"], "Needs integration")
        self.assertTrue(self.fleet.status(0)[0]["stale_heartbeat"])
        self.fleet.pulse(task["id"], "Still waiting")
        self.assertEqual(self.fleet.get(task["id"])["state"], "blocked")

    def test_release_requires_confirmed_stoppage_but_never_kills(self):
        task = self.plan()[0]
        with self.assertRaises(ValueError):
            self.fleet.release(task["id"], "cancel", False)
        with patch("agent_workflow_kit.fleet.subprocess.run") as run:
            result = self.fleet.release(task["id"], "worker stopped manually", True)
            run.assert_not_called()
        self.assertFalse(result["worker_stopped_by_tool"])
        self.assertEqual(self.plan()[0]["state"], "planned")

    def test_finish_requires_fresh_receipt_and_checks_ownership(self):
        workspace = self.workspace()
        task = self.plan([{"name": "ui", "files": ["tracked.txt"],
                           "task": "Improve", "worktree": str(workspace)}])[0]
        (workspace / "tracked.txt").write_text("changed\n")
        report = self.root / "report.md"
        report.write_text("Verified synthetic behavior\n")
        receipt = self.receipt(workspace)
        result = self.fleet.finish(task["id"], Path(receipt["receipt_path"]), report)
        self.assertEqual(result["state"], "completed")
        self.assertFalse(result["integrated"])
        self.assertEqual(self.fleet.get(task["id"])["state"], "completed")
        with self.assertRaises(ValueError):
            self.fleet.pulse(task["id"])

    def test_finish_rejects_stale_receipt(self):
        workspace = self.workspace()
        task = self.plan([{"name": "ui", "files": ["tracked.txt"],
                           "task": "Improve", "worktree": str(workspace)}])[0]
        receipt = self.receipt(workspace)
        (workspace / "tracked.txt").write_text("changed after verification")
        report = self.root / "report.md"
        report.write_text("report")
        with self.assertRaisesRegex(ValueError, "fresh"):
            self.fleet.finish(task["id"], Path(receipt["receipt_path"]), report)

    def test_finish_rejects_unowned_changes_even_with_passing_tests(self):
        workspace = self.workspace()
        task = self.plan([{"name": "ui", "files": ["tracked.txt"],
                           "task": "Improve", "worktree": str(workspace)}])[0]
        (workspace / "unowned.txt").write_text("outside ownership")
        report = self.root / "report.md"
        report.write_text("report")
        receipt = self.receipt(workspace)
        with self.assertRaisesRegex(ValueError, "unowned"):
            self.fleet.finish(task["id"], Path(receipt["receipt_path"]), report)

    def test_finish_rejects_other_repository(self):
        task = self.plan()[0]
        other = repository(self.home / "other")
        report = self.root / "report.md"
        report.write_text("report")
        receipt = self.receipt(other)
        with self.assertRaisesRegex(ValueError, "another repository"):
            self.fleet.finish(task["id"], Path(receipt["receipt_path"]), report, other)

    def test_worktree_inventory_is_deduplicated_by_repository(self):
        workspace = self.workspace()
        entries = worktrees(self.repo)
        self.assertEqual(len(entries), 2)
        self.assertIn(str(workspace.resolve()), [entry["worktree"] for entry in entries])

    def test_existing_worktree_cannot_be_shared_by_workers(self):
        workspace = self.workspace()
        with self.assertRaisesRegex(ValueError, "shared worktree"):
            self.plan([
                {"name": "a", "files": ["a"], "task": "a", "worktree": str(workspace)},
                {"name": "b", "files": ["b"], "task": "b", "worktree": str(workspace)},
            ])

    def test_backend_options_cannot_be_injected(self):
        with self.assertRaises(ValueError):
            self.plan(kind="--shared")
        task = self.plan(backend="tmux-orch", kind="droid")[0]
        preview = self.fleet.launch(task["id"])
        self.assertEqual(preview["command"][:2], ["tmux-orch", "open"])
        self.assertIn("--worktree", preview["command"])

    def test_unavailable_repository_does_not_hide_other_tasks(self):
        self.plan()
        moved = self.home / "moved-repo"
        self.repo.rename(moved)
        status = self.fleet.status()
        self.assertEqual(len(status), 1)
        self.assertIn("unavailable", status[0]["inspection_error"])


if __name__ == "__main__":
    unittest.main()
