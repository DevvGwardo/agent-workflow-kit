import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def jsonl(path, events):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n")


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True, check=True).stdout.strip()


def repository(path):
    path.mkdir(parents=True)
    git(path, "init", "-b", "main")
    # Tests use the existing environment identity; never override author config.
    (path / "tracked.txt").write_text("initial\n")
    git(path, "add", "tracked.txt")
    git(path, "diff", "--cached", "--check")
    git(path, "status", "--short")
    git(path, "commit", "-m", "test: create synthetic fixture",
        "-m", "Co-authored-by: factory-droid[bot] <138933559+factory-droid[bot]@users.noreply.github.com>")
    return path
