"""Propose tasks from git history: commits that change both code and tests.

A candidate becomes a task only if its test command fails at the parent commit
(with the commit's test files put in place) and passes at the commit itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable

from . import gitutil, procutil
from .scoring import verify_task
from .tasks import HiddenTest, Task

DEFAULT_TEST_CMD = "python -m pytest -q {test_files}"

CODE_EXTS = {
    ".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".go", ".rs", ".java", ".kt",
    ".kts", ".scala", ".cs", ".fs", ".rb", ".php", ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp",
    ".m", ".mm", ".swift", ".dart", ".lua", ".ex", ".exs", ".erl", ".clj", ".hs", ".ml",
    ".r", ".jl", ".sh", ".ps1", ".sql", ".vue", ".svelte", ".gd",
}
TEST_DIRS = {"test", "tests", "__tests__", "spec", "specs", "testing"}
TEST_NAME_PATTERNS = [
    re.compile(p)
    for p in (
        r"^test_.+\.py$",
        r"^.+_test\.py$",
        r"^.+_test\.go$",
        r"^.+\.(test|spec)\.(js|jsx|mjs|cjs|ts|tsx)$",
        r"^.+(Test|Tests)\.(java|kt|cs|scala|swift)$",
        r"^.+_spec\.rb$",
        r"^test_.+\.gd$",
    )
]
TRAILER = re.compile(r"^[A-Za-z][A-Za-z-]*-by:\s", re.IGNORECASE)
CONVENTIONAL = re.compile(r"^(\w+)(\([^)]*\))?!?:\s*")
TAG_FOR_PREFIX = {
    "fix": "bugfix", "bugfix": "bugfix", "hotfix": "bugfix",
    "feat": "feature", "feature": "feature",
    "refactor": "refactor", "perf": "performance",
}


def is_test_file(path: str) -> bool:
    p = PurePosixPath(path)
    if p.suffix.lower() not in CODE_EXTS:
        return False
    if any(part.lower() in TEST_DIRS for part in p.parts[:-1]):
        return True
    return any(rx.match(p.name) for rx in TEST_NAME_PATTERNS)


def is_code_file(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() in CODE_EXTS and not is_test_file(path)


def clean_message(message: str) -> str:
    """Commit message without trailers such as Signed-off-by or Co-Authored-By."""
    lines = [ln for ln in message.strip().splitlines() if not TRAILER.match(ln.strip())]
    return "\n".join(lines).strip()


def tags_for(subject: str) -> list[str]:
    m = CONVENTIONAL.match(subject.strip())
    if m and m.group(1).lower() in TAG_FOR_PREFIX:
        return [TAG_FOR_PREFIX[m.group(1).lower()]]
    return []


def slug(text: str, limit: int = 40) -> str:
    text = CONVENTIONAL.sub("", text.strip())
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:limit].strip("-") or "task"


@dataclass
class Candidate:
    commit: str
    parent: str
    subject: str
    message: str
    code_files: list[str]
    test_files: list[str]


def find_candidates(repo: Path, since: str | None = None, ref: str = "HEAD", max_scan: int = 500) -> list[Candidate]:
    args = ["log", "--no-merges", "--format=%H", f"-n{max_scan}"]
    if since:
        args.append(f"--since={since}")
    args.append(ref)
    shas = gitutil.git(repo, *args).split()
    out: list[Candidate] = []
    for sha in shas:
        parents = gitutil.commit_parents(repo, sha)
        if len(parents) != 1:
            continue
        changes = gitutil.changed_files(repo, parents[0], sha)
        tests = [p for st, p in changes if st in ("A", "M") and is_test_file(p)]
        code = [p for st, p in changes if is_code_file(p)]
        if not tests or not code:
            continue
        message = gitutil.git(repo, "log", "-1", "--format=%B", sha)
        cleaned = clean_message(message)
        out.append(
            Candidate(
                commit=sha,
                parent=parents[0],
                subject=cleaned.splitlines()[0] if cleaned else sha[:10],
                message=cleaned,
                code_files=code,
                test_files=tests,
            )
        )
    out.reverse()  # oldest first
    return out


def candidate_to_task(c: Candidate, repo: Path, test_cmd: str, timeout: float | None = None) -> Task:
    files = " ".join(procutil.quote_arg(f) for f in c.test_files)
    task = Task(
        id=f"{c.commit[:8]}-{slug(c.subject)}",
        repo=repo,
        base=c.parent,
        prompt=c.message or c.subject,
        test=test_cmd.replace("{test_files}", files),
        hidden_tests=[HiddenTest(path=p, from_commit=c.commit) for p in c.test_files],
        solution=c.commit,
        tags=tags_for(c.subject),
    )
    if timeout:
        task.timeout = timeout
    return task


def mine(
    repo: Path,
    since: str | None,
    test_cmd: str = DEFAULT_TEST_CMD,
    ref: str = "HEAD",
    limit: int = 20,
    max_scan: int = 500,
    log: Callable[[str], None] = print,
) -> list[Task]:
    """Return verified tasks (fail at parent, pass at commit)."""
    repo = Path(repo).resolve()
    candidates = find_candidates(repo, since, ref, max_scan)
    log(f"{len(candidates)} candidate commit(s) touch both code and tests")
    tasks: list[Task] = []
    for c in candidates:
        if len(tasks) >= limit:
            log(f"reached --limit {limit}")
            break
        task = candidate_to_task(c, repo, test_cmd)
        v = verify_task(task)
        if v.ok:
            tasks.append(task)
            log(f"  ok    {c.commit[:8]} {c.subject}")
        else:
            why = v.detail or (
                "tests already pass at parent" if v.fails_at_base is False else "tests fail at commit"
            )
            log(f"  skip  {c.commit[:8]} {c.subject}  ({why})")
    return tasks
