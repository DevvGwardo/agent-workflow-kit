"""Read-only adapters. Only user/assistant text is imported, never tool payloads."""
from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from .common import read_database, redact

SOURCES = ("claude", "droid", "codex", "cursor", "hermes", "opencode")


@dataclass
class Message:
    role: str
    text: str
    timestamp: float | None = None
    reference: str = ""


@dataclass
class Session:
    source: str
    session_id: str
    path: str
    cwd: str = ""
    title: str = ""
    messages: list[Message] = field(default_factory=list)


def timestamp(value) -> float | None:
    if isinstance(value, (int, float)):
        return float(value / 1000 if value > 1e11 else value)
    if isinstance(value, str):
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return parsed.timestamp()
        except ValueError:
            return None
    return None


def content_text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("text", "")) if value.get("type") in (
            "text", "input_text", "output_text"
        ) else ""
    if isinstance(value, list):
        return "\n".join(content_text(part) for part in value)
    return ""


def clean_text(value) -> str:
    text = content_text(value)
    query = re.search(r"<user_query>(.*?)</user_query>", text, re.S)
    if query:
        text = query.group(1)
    text = re.sub(
        r"<(system[-_]reminder|environment_context|instructions)>.*?</\1>",
        "", text, flags=re.S | re.I,
    ).strip()
    if text.startswith((
        "# AGENTS.md", "<task-notification", "<local-command",
        "[Request interrupted", "<command-name",
    )):
        return ""
    return redact(text[:65536])


def append(session: Session, role, content, ts=None, reference="") -> None:
    if role not in ("user", "assistant"):
        return
    text = clean_text(content)
    if text:
        session.messages.append(Message(role, text, timestamp(ts), reference))
        if not session.title and role == "user":
            session.title = text.splitlines()[0][:160]


def jsonl_session(source: str, path: Path) -> Session:
    session = Session(source, path.stem, str(path))
    fallback: list[tuple] = []
    responses: Counter = Counter()
    with path.open(errors="replace") as stream:
        for number, line in enumerate(stream, 1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue  # An actively written final line may be incomplete.
            if not isinstance(event, dict):
                continue
            ts = event.get("timestamp")
            reference = f"{path}:{number}"
            if source == "codex":
                payload = event.get("payload") or {}
                if not isinstance(payload, dict):
                    continue
                if event.get("type") == "session_meta":
                    session.session_id = str(payload.get("id") or
                                             payload.get("session_id") or path.stem)
                    session.cwd = str(payload.get("cwd") or "")
                elif (event.get("type") == "response_item" and
                      payload.get("type") == "message"):
                    role = payload.get("role")
                    responses[(role, clean_text(payload.get("content")))] += 1
                    append(session, role, payload.get("content"), ts, reference)
                elif event.get("type") == "event_msg":
                    role = {"user_message": "user", "agent_message": "assistant"}.get(
                        payload.get("type"), payload.get("role")
                    )
                    fallback.append((role, payload.get("message") or
                                     payload.get("content"), ts, reference))
            else:
                session.cwd = str(event.get("cwd") or session.cwd)
                session.session_id = str(event.get("sessionId") or session.session_id)
                if event.get("type") == "session_start":
                    session.session_id = str(event.get("id") or session.session_id)
                    session.title = clean_text(event.get("title"))[:160]
                message = event.get("message") or {}
                if isinstance(message, dict) and not event.get("isMeta"):
                    append(session, message.get("role"), message.get("content"),
                           ts, reference)
    for role, content, ts, reference in fallback:
        key = (role, clean_text(content))
        if responses[key]:
            responses[key] -= 1
        else:
            append(session, role, content, ts, reference)
    if session.messages and all(message.timestamp is not None for message in session.messages):
        session.messages.sort(key=lambda message: message.timestamp)
    return session


def cursor_session(path: Path) -> Session:
    session = Session("cursor", path.parent.name, str(path))
    conn = read_database(path)
    try:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if "meta" in tables:
            meta = dict(conn.execute("SELECT key,value FROM meta"))
            cwd = meta.get("cwd") or meta.get("workingDirectory") or ""
            session.cwd = cwd.decode(errors="replace") if isinstance(cwd, bytes) else str(cwd)
        for row in conn.execute("SELECT rowid,data FROM blobs ORDER BY rowid"):
            try:
                message = json.loads(row["data"])
            except (ValueError, TypeError, UnicodeDecodeError):
                continue
            if not isinstance(message, dict):
                continue
            session.cwd = str(message.get("cwd") or session.cwd)
            append(session, message.get("role"), message.get("content"),
                   message.get("timestamp") or message.get("ts"),
                   f"{path}#blob-row={row['rowid']}")
    finally:
        conn.close()
    return session


def candidates(home: Path, source: str, since: float, limit: int) -> list[Path]:
    roots = {
        "claude": (home / ".claude/projects", "*/*.jsonl"),
        "droid": (home / ".factory/sessions", "*/*.jsonl"),
        "codex": (home / ".codex/sessions", "**/rollout-*.jsonl"),
        "cursor": (home / ".cursor", "chats/*/*/store.db"),
    }
    root, pattern = roots[source]
    paths = list(root.glob(pattern))
    if source == "cursor":
        paths.extend(root.glob("acp-sessions/*/store.db"))
    paths = [path for path in paths if path.is_file()
             and not path.name.endswith(".settings.json")
             and path.stat().st_mtime >= since]
    paths.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return paths[:limit] if limit else paths


def shared_sessions(home: Path, source: str, since: float, limit: int) -> Iterator[Session]:
    path = home / (".hermes/state.db" if source == "hermes"
                   else ".local/share/opencode/opencode.db")
    if not path.exists():
        return
    conn = read_database(path)
    try:
        if source == "hermes":
            sessions = conn.execute(
                "SELECT id,cwd,title FROM sessions WHERE started_at>=? "
                "ORDER BY started_at DESC LIMIT ?", (since, limit or -1)
            ).fetchall()
            for row in sessions:
                session = Session(source, row["id"], str(path),
                                  row["cwd"] or "", clean_text(row["title"])[:160])
                for message in conn.execute(
                    "SELECT id,role,content,timestamp FROM messages "
                    "WHERE session_id=? AND role IN ('user','assistant') ORDER BY id",
                    (row["id"],),
                ):
                    append(session, message["role"], message["content"],
                           message["timestamp"], f"{path}#message={message['id']}")
                yield session
        else:
            sessions = conn.execute(
                "SELECT id,directory,title FROM session WHERE time_updated>=? "
                "ORDER BY time_updated DESC LIMIT ?", (since * 1000, limit or -1)
            ).fetchall()
            for row in sessions:
                session = Session(source, row["id"], str(path),
                                  row["directory"] or "", clean_text(row["title"])[:160])
                for message in conn.execute(
                    "SELECT p.id,p.data,p.time_created,m.data AS message_data FROM part p "
                    "JOIN message m ON m.id=p.message_id "
                    "WHERE p.session_id=? ORDER BY p.time_created,p.id", (row["id"],),
                ):
                    try:
                        part = json.loads(message["data"])
                        parent = json.loads(message["message_data"])
                    except (ValueError, TypeError):
                        continue
                    if (isinstance(part, dict) and isinstance(parent, dict) and
                            part.get("type") == "text" and not part.get("synthetic")):
                        append(session, parent.get("role"), part.get("text"),
                               message["time_created"], f"{path}#part={message['id']}")
                yield session
    finally:
        conn.close()
