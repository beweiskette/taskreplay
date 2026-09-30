"""Git helpers. Nothing here writes to the user's main working tree.

`git worktree add` stores a small bookkeeping entry under `.git/worktrees/`,
which `remove_worktree` deletes again. Files in the main checkout are never
touched.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator


class GitError(RuntimeError):
    pass


def git(repo: str | os.PathLike, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def resolve_commit(repo: str | os.PathLike, ref: str) -> str:
    return git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()


def is_repo(path: str | os.PathLike) -> bool:
    try:
        git(path, "rev-parse", "--git-dir")
        return True
    except (GitError, FileNotFoundError, NotADirectoryError):
        return False


def _on_rm_error(func, path, _exc):
    # Git marks object files read-only on Windows; make them writable and retry.
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def rmtree(path: str | os.PathLike) -> None:
    if not os.path.exists(path):
        return
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_on_rm_error)
    else:
        shutil.rmtree(path, onerror=_on_rm_error)


def add_worktree(repo: str | os.PathLike, commit: str, parent_dir: str | os.PathLike | None = None) -> Path:
    """Create a detached worktree of `commit` in a fresh temp directory."""
    holder = Path(tempfile.mkdtemp(prefix="taskreplay-", dir=parent_dir))
    wt = holder / "wt"
    git(repo, "worktree", "add", "--detach", "--quiet", str(wt), commit)
    return wt


def remove_worktree(repo: str | os.PathLike, wt: str | os.PathLike) -> None:
    wt = Path(wt)
    git(repo, "worktree", "remove", "--force", str(wt), check=False)
    rmtree(wt.parent)
    git(repo, "worktree", "prune", check=False)


@contextmanager
def worktree(repo: str | os.PathLike, commit: str, keep: bool = False) -> Iterator[Path]:
    wt = add_worktree(repo, commit)
    try:
        yield wt
    finally:
        if not keep:
            remove_worktree(repo, wt)


@dataclass
class DiffStats:
    files: list[str] = field(default_factory=list)
    added: int = 0
    removed: int = 0
    patch: str = ""


def diff_against(wt: str | os.PathLike, base: str) -> DiffStats:
    """Stage everything in the worktree and diff it against `base`.

    Uses the worktree's own index, so the main checkout is unaffected.
    """
    git(wt, "add", "-A", "--", ".")
    numstat = git(wt, "diff", "--cached", "--numstat", "--no-renames", base)
    stats = DiffStats()
    for line in numstat.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        a, r, path = parts
        stats.files.append(path)
        if a.isdigit():
            stats.added += int(a)
        if r.isdigit():
            stats.removed += int(r)
    stats.patch = git(wt, "diff", "--cached", "--no-renames", base)
    return stats


def checkout_files_from(wt: str | os.PathLike, commit: str, paths: list[str]) -> None:
    """Overwrite `paths` in the worktree with their content at `commit`."""
    if paths:
        git(wt, "checkout", commit, "--", *paths)


def commit_parents(repo: str | os.PathLike, commit: str) -> list[str]:
    line = git(repo, "rev-list", "--parents", "-n", "1", commit).split()
    return line[1:]


def changed_files(repo: str | os.PathLike, parent: str, commit: str) -> list[tuple[str, str]]:
    """Return (status, path) pairs changed between parent and commit."""
    out = git(repo, "diff", "--name-status", "--no-renames", parent, commit)
    result = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            result.append((parts[0][:1], parts[-1]))
    return result
