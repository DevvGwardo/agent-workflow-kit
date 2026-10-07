import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from support import jsonl
from agent_workflow_kit.common import redact, sanitize, state_root, write_private_json
from agent_workflow_kit.history import History
from agent_workflow_kit.sources import (
    Message, Session, clean_text, cursor_session, jsonl_session,
    shared_sessions, timestamp,
)


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.history = History(state_root(self.home / "state"))

    def tearDown(self):
        self.history.close()
        self.tmp.cleanup()

    def test_claude_excludes_tools_and_instructions(self):
        path = self.home / ".claude/projects/example/session.jsonl"
        jsonl(path, [
            {"type": "user", "sessionId": "s1", "cwd": "/project",
             "timestamp": "2026-10-01T00:00:00Z", "message": {"role": "user", "content": [
                 {"type": "text", "text": "<system-reminder>injected</system-reminder>Fix the browser"},
                 {"type": "tool_result", "content": "tool secret payload"}]}},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "hidden reasoning"},
                {"type": "tool_use", "name": "Bash", "input": {"command": "private"}},
                {"type": "text", "text": "Next: smoke test the fix"}]}},
            {"type": "user", "isMeta": True, "message": {"role": "user", "content": "meta"}},
        ])
        session = jsonl_session("claude", path)
        self.assertEqual(session.session_id, "s1")
        self.assertEqual(session.cwd, "/project")
        self.assertEqual([m.text for m in session.messages], ["Fix the browser", "Next: smoke test the fix"])
        self.history.store(session)
        self.assertEqual(len(self.history.search("browser")), 1)
        self.assertEqual(self.history.search("injected"), [])
        self.assertEqual(self.history.search("secret"), [])

    def test_droid_reads_native_shape_and_tolerates_partial_tail(self):
        path = self.home / "droid.jsonl"
        jsonl(path, [
            {"type": "session_start", "id": "d1", "cwd": "/repo", "title": "Repair"},
            {"type": "message", "message": {"role": "user", "content": [
                {"type": "text", "text": "Repair the gateway"}]}},
        ])
        with path.open("a") as stream:
            stream.write('{"type":')
        session = jsonl_session("droid", path)
        self.assertEqual(session.session_id, "d1")
        self.assertEqual(session.messages[0].text, "Repair the gateway")

    def test_codex_uses_response_text_without_duplicate_event(self):
        path = self.home / "codex.jsonl"
        jsonl(path, [
            {"type": "session_meta", "payload": {"id": "c1", "cwd": "/repo"}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "Fix tests"}},
            {"type": "response_item", "payload": {"type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "Fix tests"}]}},
            {"type": "response_item", "payload": {"type": "function_call_output", "output": "never import"}},
        ])
        session = jsonl_session("codex", path)
        self.assertEqual(session.session_id, "c1")
        self.assertEqual(len(session.messages), 1)
        self.assertEqual(session.messages[0].text, "Fix tests")

    def test_codex_event_only_fallback(self):
        path = self.home / "event.jsonl"
        jsonl(path, [{"type": "event_msg", "payload": {
            "type": "agent_message", "message": "Completed tests"}}])
        self.assertEqual(jsonl_session("codex", path).messages[0].role, "assistant")

    def test_codex_preserves_unmatched_event_messages_in_mixed_formats(self):
        path = self.home / "mixed.jsonl"
        jsonl(path, [
            {"type": "event_msg", "timestamp": "2026-10-01T00:00:00Z",
             "payload": {"type": "user_message", "message": "Earlier request"}},
            {"type": "response_item", "timestamp": "2026-10-01T00:01:00Z",
             "payload": {"type": "message", "role": "user", "content": [
                 {"type": "input_text", "text": "Later request"}]}},
        ])
        self.assertEqual([m.text for m in jsonl_session("codex", path).messages],
                         ["Earlier request", "Later request"])

    def test_cursor_extracts_query_without_injected_reminder(self):
        path = self.home / ".cursor/chats/chat/thread/store.db"
        path.parent.mkdir(parents=True)
        conn = sqlite3.connect(path)
        conn.executescript("CREATE TABLE blobs (data BLOB); CREATE TABLE meta (key TEXT,value TEXT);")
        conn.execute("INSERT INTO meta VALUES('cwd','/cursor-project')")
        conn.execute("INSERT INTO blobs VALUES(?)", (json.dumps({
            "role": "user", "content": "<system_reminder>secret injection</system_reminder>"
            "<user_query>Test the app</user_query>"}).encode(),))
        conn.commit()
        conn.close()
        original = path.read_bytes()
        session = cursor_session(path)
        self.assertEqual(session.messages[0].text, "Test the app")
        self.assertEqual(session.cwd, "/cursor-project")
        self.assertEqual(path.read_bytes(), original)

    def test_hermes_readonly_adapter(self):
        path = self.home / ".hermes/state.db"
        path.parent.mkdir(parents=True)
        conn = sqlite3.connect(path)
        conn.executescript(
            "CREATE TABLE sessions(id TEXT,cwd TEXT,title TEXT,started_at REAL);"
            "CREATE TABLE messages(id INTEGER,session_id TEXT,role TEXT,content TEXT,timestamp REAL);"
        )
        conn.execute("INSERT INTO sessions VALUES('h1','/repo','Hermes test',?)", (time.time(),))
        conn.executemany("INSERT INTO messages VALUES(?,?,?,?,?)", [
            (1, "h1", "user", "Test Hermes", time.time()),
            (2, "h1", "tool", "ignored output", time.time()),
        ])
        conn.commit()
        conn.close()
        original = path.read_bytes()
        sessions = list(shared_sessions(self.home, "hermes", 0, 20))
        self.assertEqual(len(sessions[0].messages), 1)
        self.assertEqual(path.read_bytes(), original)

    def test_opencode_adapter_filters_synthetic_and_tools(self):
        path = self.home / ".local/share/opencode/opencode.db"
        path.parent.mkdir(parents=True)
        conn = sqlite3.connect(path)
        conn.executescript(
            "CREATE TABLE session(id TEXT,directory TEXT,title TEXT,time_updated REAL);"
            "CREATE TABLE message(id TEXT,data TEXT);"
            "CREATE TABLE part(id TEXT,message_id TEXT,session_id TEXT,data TEXT,time_created REAL);"
        )
        conn.execute("INSERT INTO session VALUES('o1','/repo','OpenCode test',?)", (time.time() * 1000,))
        conn.execute("INSERT INTO message VALUES('m1',?)", (json.dumps({"role": "user"}),))
        for i, part in enumerate([
            {"type": "text", "text": "Real request"},
            {"type": "text", "text": "Synthetic instructions", "synthetic": True},
            {"type": "tool", "text": "Tool output"},
        ]):
            conn.execute("INSERT INTO part VALUES(?,?,?,?,?)",
                         (str(i), "m1", "o1", json.dumps(part), time.time() * 1000))
        conn.commit()
        conn.close()
        session = list(shared_sessions(self.home, "opencode", 0, 10))[0]
        self.assertEqual([m.text for m in session.messages], ["Real request"])

    def test_incremental_index_replaces_changed_session(self):
        path = self.home / ".claude/projects/project/s1.jsonl"
        jsonl(path, [{"message": {"role": "user", "content": "first fix"}, "sessionId": "s1"}])
        self.assertEqual(self.history.index(self.home, ["claude"], 0, 0)["claude"]["updated"], 1)
        self.assertEqual(self.history.index(self.home, ["claude"], 0, 0)["claude"]["unchanged"], 1)
        jsonl(path, [{"message": {"role": "user", "content": "second browser fix"}, "sessionId": "s1"}])
        self.assertEqual(self.history.index(self.home, ["claude"], 0, 0)["claude"]["updated"], 1)
        self.assertEqual(self.history.search("first"), [])
        self.assertEqual(len(self.history.search("browser")), 1)
        self.assertEqual(self.history.stats()["messages"], 1)

    def test_search_is_literal_and_project_filtered(self):
        self.history.store(Session("droid", "s", "/log", "/project", messages=[
            Message("user", "Browser fix with brackets", 123)]))
        self.assertEqual(len(self.history.search("browser!!!", project="project")), 1)
        self.assertEqual(self.history.search("browser", project="other"), [])
        with self.assertRaises(ValueError):
            self.history.search("***")

    def test_recovery_warns_and_preserves_references(self):
        self.history.store(Session("claude", "s", "/log", messages=[
            Message("assistant", "Next: test release", reference="/log:4")]))
        recovered = self.history.recover("claude", "s")
        self.assertIn("Untrusted", recovered["warning"])
        self.assertEqual(recovered["candidate_handoffs"][0]["reference"], "/log:4")

    def test_malformed_source_reports_error_without_crashing_other_sources(self):
        path = self.home / ".hermes/state.db"
        path.parent.mkdir(parents=True)
        path.write_text("not sqlite")
        report = self.history.index(self.home, ["hermes", "opencode"], 0, 10)
        self.assertTrue(report["hermes"]["errors"])
        self.assertFalse(report["opencode"]["errors"])

    def test_best_effort_secret_redaction(self):
        value = "sk-" + "x" * 24
        self.assertNotIn(value, redact("value: " + value))
        self.assertNotIn("hunter", redact('API_KEY="hunter"'))
        self.assertEqual(clean_text([{"type": "tool_result", "content": "private"}]), "")
        self.assertNotIn("abc", redact("https://user:abc@example.invalid"))

    def test_timestamps_normalize_seconds_millis_and_iso(self):
        self.assertEqual(timestamp(1700000000000), timestamp(1700000000))
        self.assertEqual(timestamp("2026-01-01T00:00:00"), timestamp("2026-01-01T00:00:00Z"))
        self.assertIsNone(timestamp("not time"))

    def test_redaction_is_idempotent_and_preserves_json(self):
        self.assertEqual(redact(redact("API_KEY=fixture")), redact("API_KEY=fixture"))
        value = {"token": "a normal string", "output": 'API_KEY="fixture"'}
        output = json.loads(json.dumps(sanitize(value)))
        self.assertEqual(output["token"], "a normal string")
        self.assertNotIn("fixture", output["output"])
        path = self.home / "state/example.json"
        write_private_json(path, value)
        self.assertEqual(json.loads(path.read_text()), output)

    def test_private_key_block_redaction(self):
        delimiter = "-" * 5
        key_type = "TEST PRIVATE KEY"
        header = delimiter + "BEGIN " + key_type + delimiter
        footer = delimiter + "END " + key_type + delimiter
        value = header + "\nsynthetic fixture body\n" + footer
        self.assertEqual(redact(value), "[REDACTED PRIVATE KEY]")

    @unittest.skipUnless(os.name == "posix", "POSIX file permissions")
    def test_runtime_files_are_private(self):
        self.assertEqual((self.home / "state").stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.home / "state/history.db").stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
