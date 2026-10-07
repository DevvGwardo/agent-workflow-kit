from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from .common import emit, state_root
from .history import History
from .fleet import Fleet
from .sources import SOURCES
from .verification import Verification


def fleet_command(args) -> int:
    fleet = Fleet(state_root(args.state))
    try:
        if args.operation == "plan":
            path = Path(args.file).expanduser().resolve()
            result = fleet.plan(json.loads(path.read_text()), path.parent)
        elif args.operation == "launch":
            result = fleet.launch(args.task_id, args.execute)
        elif args.operation == "status":
            result = fleet.status(args.stale_after_minutes)
        elif args.operation == "recover":
            result = fleet.recover(args.task_id)
        elif args.operation == "pulse":
            result = fleet.pulse(args.task_id, args.note)
        elif args.operation == "block":
            result = fleet.block(args.task_id, args.reason)
        elif args.operation == "release":
            result = fleet.release(args.task_id, args.reason, args.confirm_stopped)
        else:
            result = fleet.finish(
                args.task_id, Path(args.receipt).expanduser().resolve(),
                Path(args.report).expanduser().resolve(),
                Path(args.worktree).expanduser().resolve() if args.worktree else None,
            )
        emit(result)
        return 1 if isinstance(result, dict) and result.get("state") == "failed" else 0
    finally:
        fleet.close()


def verify_command(args) -> int:
    project = Path(args.project).expanduser().resolve()
    gates = Path(args.gates).expanduser()
    if not gates.is_absolute():
        gates = project / gates
    verification = Verification(state_root(args.state))
    result = (verification.run(project, gates) if args.operation == "run"
              else verification.status(project, gates, args.max_age_hours * 3600))
    emit(result)
    return 0 if result["passed"] else 1


def history_command(args) -> int:
    history = History(state_root(args.state))
    try:
        if args.operation in ("index", "watch"):
            iteration = 0
            while True:
                result = history.index(Path(args.home).expanduser(), args.sources,
                                       args.since_days, args.limit_sessions)
                emit(result)
                iteration += 1
                if args.operation == "index":
                    return int(any(value["errors"] for value in result.values()))
                if args.iterations and iteration >= args.iterations:
                    return int(any(value["errors"] for value in result.values()))
                time.sleep(args.interval)
        elif args.operation == "search":
            emit(history.search(args.query, args.source, args.project, args.limit))
        elif args.operation == "recover":
            emit(history.recover(args.source, args.session_id, args.limit))
        else:
            emit(history.stats())
        return 0
    finally:
        history.close()


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="awk-kit", description="Local tools for cross-agent coding workflows"
    )
    root.add_argument("--state", help="Private runtime directory, outside the repository")
    commands = root.add_subparsers(dest="command", required=True)
    history = commands.add_parser("history", help="Search and recover cross-agent sessions")
    history.set_defaults(handler=history_command)
    operations = history.add_subparsers(dest="operation", required=True)
    for name in ("index", "watch"):
        command = operations.add_parser(name)
        command.add_argument("--home", default=str(Path.home()), help="Agent history home")
        command.add_argument("--sources", nargs="+", choices=SOURCES, default=list(SOURCES))
        command.add_argument("--since-days", type=float, default=30,
                             help="Lookback window; 0 includes all history")
        command.add_argument("--limit-sessions", type=int, default=200,
                             help="Maximum sessions per source; 0 means unlimited")
        if name == "watch":
            command.add_argument("--interval", type=float, default=60)
            command.add_argument("--iterations", type=int, default=0,
                                 help="0 watches until interrupted")
    search = operations.add_parser("search")
    search.add_argument("query")
    search.add_argument("--source", choices=SOURCES)
    search.add_argument("--project", help="Substring of the original working directory")
    search.add_argument("--limit", type=int, default=10)
    recover = operations.add_parser("recover")
    recover.add_argument("source", choices=SOURCES)
    recover.add_argument("session_id")
    recover.add_argument("--limit", type=int, default=12)
    operations.add_parser("stats")
    verify = commands.add_parser("verify", help="Run commit-bound acceptance gates")
    verify.set_defaults(handler=verify_command)
    verify_operations = verify.add_subparsers(dest="operation", required=True)
    for name in ("run", "status"):
        command = verify_operations.add_parser(name)
        command.add_argument("--project", default=".")
        command.add_argument("--gates", default="agent-gates.json")
        if name == "status":
            command.add_argument("--max-age-hours", type=float, default=24)
    fleet = commands.add_parser("fleet", help="Plan ownership and coordinate existing launchers")
    fleet.set_defaults(handler=fleet_command)
    fleet_operations = fleet.add_subparsers(dest="operation", required=True)
    plan = fleet_operations.add_parser("plan")
    plan.add_argument("file", help="JSON fleet plan; registering does not launch workers")
    status = fleet_operations.add_parser("status")
    status.add_argument("--stale-after-minutes", type=float, default=30)
    for name in ("launch", "recover", "pulse", "block", "release", "finish"):
        command = fleet_operations.add_parser(name)
        command.add_argument("task_id")
        if name == "launch":
            command.add_argument("--execute", action="store_true", help="Explicitly start the worker")
        elif name == "pulse":
            command.add_argument("--note", default="")
        elif name in ("block", "release"):
            command.add_argument("--reason", required=True)
            if name == "release":
                command.add_argument("--confirm-stopped", action="store_true")
        elif name == "finish":
            command.add_argument("--receipt", required=True)
            command.add_argument("--report", required=True)
            command.add_argument("--worktree")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.operation == "watch" and (args.interval <= 0 or args.iterations < 0):
            raise ValueError("Watch interval must be positive and iterations nonnegative")
        return args.handler(args)
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, sqlite3.Error, subprocess.TimeoutExpired) as error:
        # Don't print arbitrary source content or exception payloads from SQLite.
        if isinstance(error, ValueError):
            emit({"error": str(error)})
        else:
            emit({"error": type(error).__name__, "message": "Operation failed; check paths and permissions"})
        return 1


if __name__ == "__main__":
    sys.exit(main())
