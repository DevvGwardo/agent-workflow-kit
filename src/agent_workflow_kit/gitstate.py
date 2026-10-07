from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from pathlib import Path


def git(path: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, timeout=30
    )
    if result.returncode:
        raise ValueError("A Git repository with an initial commit is required")
    return result.stdout


def identity(path: Path) -> dict:
    root = Path(os.fsdecode(git(path, "rev-parse", "--show-toplevel")).strip()).resolve()
    common = Path(os.fsdecode(git(root, "rev-parse", "--git-common-dir")).strip())
    if not common.is_absolute():
        common = root / common
    return {
        "root": str(root),
        "repository_id": str(common.resolve()),
        "commit": git(root, "rev-parse", "HEAD").decode().strip(),
        "branch": git(root, "rev-parse", "--abbrev-ref", "HEAD").decode().strip(),
    }


def fingerprint(path: Path) -> dict:
    result = identity(path)
    root = Path(result["root"])
    status = git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    index = git(root, "ls-files", "--stage", "-z")
    names = set(git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split(b"\0"))
    digest = hashlib.sha256()
    for name in sorted(names - {b""}):
        target = root / os.fsdecode(name)
        digest.update(name + b"\0")
        if not target.exists() and not target.is_symlink():
            digest.update(b"missing\0")
            continue
        metadata = target.lstat()
        digest.update(str(stat.S_IMODE(metadata.st_mode)).encode() + b"\0")
        if target.is_symlink():
            digest.update(b"symlink\0" + os.fsencode(os.readlink(target)))
        elif target.is_file():
            with target.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
        else:
            raise ValueError("Submodules and non-file tracked entries are not supported")
        digest.update(b"\0")
    result.update({
        "dirty": bool(status),
        "tree_digest": digest.hexdigest(),
        "status_digest": hashlib.sha256(status).hexdigest(),
        "index_digest": hashlib.sha256(index).hexdigest(),
    })
    return result


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
