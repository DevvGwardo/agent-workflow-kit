from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any


def state_root(path: str | Path | None = None) -> Path:
    root = Path(path or os.environ.get(
        "AGENT_WORKFLOW_STATE", "~/.local/state/agent-workflow-kit"
    )).expanduser()
    if root.is_symlink():
        raise ValueError("The state directory must not be a symlink")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    return root.resolve()


def database(path: Path) -> sqlite3.Connection:
    if path.is_symlink():
        raise ValueError("The state database must not be a symlink")
    # Create with private permissions, rather than chmod after writing data.
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    path.chmod(0o600)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def read_database(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def redact(text: str) -> str:
    """Best-effort defense, not a guarantee that private content is removed."""
    private_key_block = (
        r"-----BEGIN "
        r"[^-\n]*PRIVATE KEY-----.*?"
        r"-----END "
        r"[^-\n]*PRIVATE KEY-----"
    )
    text = re.sub(
        private_key_block,
        "[REDACTED PRIVATE KEY]", text, flags=re.S,
    )
    text = re.sub(
        r"\b(?:sk-(?:proj-|ant-)?[A-Za-z0-9_-]{12,}|"
        r"gh[pousr]_[A-Za-z0-9]{15,}|github_pat_[A-Za-z0-9_]{15,})\b",
        "[REDACTED TOKEN]", text,
    )
    text = re.sub(r"(?i)(\bBearer\s+)\S+", r"\1[REDACTED]", text)
    text = re.sub(
        r"""(?i)(\b[\w.-]*(?:api[_-]?key|token|secret|password|credential)[\w.-]*"""
        r"""["']?\s*[:=]\s*)(?:"[^"\n]*"|'[^'\n]*'|\[REDACTED\]|[^\s,;}\]]+)""",
        r"\1[REDACTED]", text,
    )
    text = re.sub(
        r"(https?://)[^/\s:@]+:[^/\s@]+@", r"\1[REDACTED]@", text
    )
    return text


def emit(value: Any) -> None:
    print(json.dumps(sanitize(value), indent=2, ensure_ascii=False, default=str))


def sanitize(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {key: sanitize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(item) for item in value]
    return value


def write_private_json(path: Path, value: Any) -> None:
    _write_private(path, json.dumps(sanitize(value), indent=2, ensure_ascii=False) + "\n")


def write_private_text(path: Path, text: str) -> None:
    _write_private(path, redact(text))


def _write_private(path: Path, text: str) -> None:
    """Atomically replace an agent-owned receipt with a private file."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ValueError("Refusing to overwrite a symlink")
    import tempfile

    fd, name = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
