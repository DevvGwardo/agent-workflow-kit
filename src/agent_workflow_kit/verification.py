from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from .common import redact, write_private_json
from .gitstate import file_digest, fingerprint

PHASES = ("implemented", "integrated", "released")


def inside(root: Path, name: str) -> Path:
    path = (root / name).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Gate working directories and artifacts must stay inside the project")
    return path


def load_gates(path: Path, project: Path) -> tuple[dict, str]:
    raw = path.read_bytes()
    try:
        config = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as error:
        raise ValueError("Gates must be a valid UTF-8 JSON file") from error
    if not isinstance(config, dict) or config.get("version") != 1:
        raise ValueError("Gate configuration requires version: 1")
    checks = config.get("checks")
    if not isinstance(checks, list) or not checks:
        raise ValueError("Gate configuration must contain at least one check")
    seen = set()
    for check in checks:
        if not isinstance(check, dict):
            raise ValueError("Each check must be an object")
        name = check.get("id", "")
        if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", name) or name in seen:
            raise ValueError("Check IDs must be unique simple names")
        seen.add(name)
        command = check.get("command")
        if (not isinstance(command, list) or not command or
                any(not isinstance(arg, str) or "\0" in arg for arg in command) or not command[0]):
            raise ValueError("Check commands must be nonempty argument arrays")
        if Path(command[0]).name.lower() in ("sh", "bash", "zsh", "fish", "cmd", "powershell", "pwsh"):
            raise ValueError("Shell wrappers are not allowed; use an executable argument array")
        if check.get("phase", "implemented") not in PHASES:
            raise ValueError("Phase must be implemented, integrated, or released")
        timeout = check.get("timeout_seconds", 120)
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= 3600:
            raise ValueError("Check timeouts must be between 0 and 3600 seconds")
        if not isinstance(check.get("required", True), bool):
            raise ValueError("required must be a boolean")
        cwd = check.get("cwd", ".")
        if not isinstance(cwd, str) or not inside(project, cwd).is_dir():
            raise ValueError("Gate working directory must be an existing project directory")
        pattern = check.get("expect")
        if pattern is not None:
            if not isinstance(pattern, str):
                raise ValueError("expect must be a regular-expression string")
            try:
                re.compile(pattern)
            except re.error as error:
                raise ValueError("Invalid expectation regular expression") from error
        artifacts = check.get("artifacts", [])
        if not isinstance(artifacts, list) or any(not isinstance(item, str) for item in artifacts):
            raise ValueError("artifacts must be a list of project-relative paths")
        for item in artifacts:
            inside(project, item)
    return config, hashlib.sha256(raw).hexdigest()


def run_check(check: dict, project: Path) -> dict:
    started = time.time()
    result = {
        "id": check["id"], "phase": check.get("phase", "implemented"),
        "required": check.get("required", True),
        "executable": Path(check["command"][0]).name,
        "command_digest": hashlib.sha256(json.dumps(check["command"]).encode()).hexdigest(),
        "cwd": str(inside(project, check.get("cwd", "."))),
        "started_at": started, "artifacts": [],
    }
    with tempfile.TemporaryFile() as output:
        try:
            process = subprocess.Popen(
                check["command"], cwd=result["cwd"], stdout=output,
                stderr=subprocess.STDOUT, start_new_session=os.name == "posix",
            )
            try:
                process.wait(timeout=check.get("timeout_seconds", 120))
                result["exit_code"] = process.returncode
                result["state"] = "passed" if process.returncode == 0 else "failed"
            except subprocess.TimeoutExpired:
                if os.name == "posix":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    process.kill()
                process.wait()
                result.update(state="timed_out", exit_code=process.returncode)
        except OSError:
            result.update(state="failed", exit_code=None, error="Executable could not be started")
        size = output.seek(0, os.SEEK_END)
        output.seek(max(0, size - 65536))
        tail = output.read().decode(errors="replace")
    result["output_tail"] = redact(tail[-4000:])
    result["output_truncated"] = size > 4000
    if check.get("expect") is not None and result["state"] == "passed":
        if not re.search(check["expect"], tail):
            result.update(state="failed", error="Expected pattern absent from final 64 KiB of output")
    for name in check.get("artifacts", []):
        path = inside(project, name)
        if not path.is_file() or path.stat().st_size == 0:
            result.update(state="failed", error="Expected evidence artifact is missing or empty")
        else:
            result["artifacts"].append({"path": str(path), "sha256": file_digest(path)})
    result["duration_seconds"] = round(time.time() - started, 3)
    return result


def phase_states(checks: list[dict]) -> dict:
    phases = {}
    for phase in PHASES:
        selected = [check for check in checks if check["phase"] == phase]
        if not selected:
            phases[phase] = "not_configured"
        elif any(check["state"] != "passed" and check["required"] for check in selected):
            phases[phase] = "failed"
        elif any(check["state"] != "passed" for check in selected):
            phases[phase] = "passed_with_optional_failures"
        else:
            phases[phase] = "passed"
    return phases


class Verification:
    def __init__(self, root: Path):
        self.root = root

    def run(self, project: Path, gates: Path) -> dict:
        before = fingerprint(project)
        project = Path(before["root"])
        if self.root.is_relative_to(project):
            raise ValueError("Verification state must be outside the project")
        config, config_digest = load_gates(gates, project)
        checks = []
        for check in config["checks"]:
            checks.append(run_check(check, project))
            # Stop when a gate modifies attested files; don't verify a moving target.
            if fingerprint(project) != before:
                break
        after = fingerprint(project)
        unchanged = before == after
        config_unchanged = gates.is_file() and file_digest(gates) == config_digest
        for check in config["checks"][len(checks):]:
            checks.append({
                "id": check["id"], "phase": check.get("phase", "implemented"),
                "required": check.get("required", True), "state": "skipped",
                "reason": "Git state changed during an earlier gate",
            })
        passed = unchanged and config_unchanged and all(
            check["state"] == "passed" for check in checks if check["required"]
        )
        receipt = {
            "version": 1, "id": uuid.uuid4().hex, "created_at": time.time(),
            "project": str(project), "gates": str(gates.resolve()),
            "config_digest": config_digest, "git_before": before, "git_after": after,
            "git_unchanged": unchanged, "config_unchanged": config_unchanged,
            "passed": passed, "phases": phase_states(checks), "checks": checks,
            "limitations": "Ignored files, environment values, and remote state are not attested.",
        }
        directory = self.root / "verification" / hashlib.sha256(str(project).encode()).hexdigest()[:20]
        path = directory / (receipt["id"] + ".json")
        write_private_json(path, receipt)
        receipt["receipt_path"] = str(path)
        return receipt

    def status(self, project: Path, gates: Path, max_age: float = 86400) -> dict:
        if max_age < 0:
            raise ValueError("Receipt maximum age must be nonnegative")
        current = fingerprint(project)
        project = Path(current["root"])
        _, config_digest = load_gates(gates, project)
        directory = self.root / "verification" / hashlib.sha256(str(project).encode()).hexdigest()[:20]
        paths = sorted(directory.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
        receipt = None
        chosen = None
        for path in paths:
            try:
                value = json.loads(path.read_text())
            except (ValueError, OSError):
                continue
            if isinstance(value, dict) and value.get("gates") == str(gates.resolve()):
                receipt, chosen = value, path
                break
        if receipt is None:
            return {"state": "missing", "passed": False, "project": str(project)}
        reasons = []
        if receipt.get("git_after") != current:
            reasons.append("Git state changed")
        if receipt.get("config_digest") != config_digest:
            reasons.append("Gate configuration changed")
        if time.time() - receipt["created_at"] > max_age:
            reasons.append("Evidence exceeded maximum age")
        for check in receipt["checks"]:
            for artifact in check.get("artifacts", []):
                path = Path(artifact["path"])
                if (not path.is_relative_to(project) or not path.is_file() or
                        file_digest(path) != artifact["sha256"]):
                    reasons.append("Evidence artifact changed or disappeared")
        stale = bool(reasons)
        return {
            "state": "stale" if stale else ("passed" if receipt["passed"] else "failed"),
            "passed": receipt["passed"] and not stale, "stale_reasons": sorted(set(reasons)),
            "receipt_path": str(chosen), "created_at": receipt["created_at"],
            "git": current,
            "phases": {phase: "stale" for phase in PHASES} if stale else receipt["phases"],
            "checks": [{"id": check["id"], "state": check["state"]} for check in receipt["checks"]],
        }
