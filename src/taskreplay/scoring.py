"""Scoring: place hidden tests, run the task's test command."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from . import gitutil, procutil
from .tasks import Task


@dataclass
class CheckOutcome:
    passed: bool
    exit_code: int | None
    elapsed: float
    timed_out: bool
    output_tail: str


def apply_hidden_tests(task: Task, workdir: Path) -> None:
    """Put hidden test files into the worktree, overwriting agent edits to them."""
    for h in task.hidden_tests:
        dest = (workdir / h.path).resolve()
        if workdir.resolve() not in dest.parents:
            raise ValueError(f"hidden test path escapes the worktree: {h.path}")
        if h.from_commit:
            gitutil.checkout_files_from(workdir, h.from_commit, [h.path])
        else:
            src = (task.source_dir / str(h.from_file)).resolve()
            if not src.is_file():
                raise FileNotFoundError(f"hidden test file not found: {h.from_file}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)


def run_tests(task: Task, workdir: Path, tail_chars: int = 2000) -> CheckOutcome:
    proc = procutil.run(task.test, cwd=workdir, timeout=task.test_timeout, shell=True)
    output = (proc.stdout + ("\n" + proc.stderr if proc.stderr else "")).strip()
    return CheckOutcome(
        passed=(not proc.timed_out and proc.returncode == 0),
        exit_code=proc.returncode,
        elapsed=round(proc.elapsed, 3),
        timed_out=proc.timed_out,
        output_tail=output[-tail_chars:],
    )


@dataclass
class Verification:
    task_id: str
    fails_at_base: bool | None
    passes_at_solution: bool | None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.fails_at_base) and self.passes_at_solution is not False


def verify_task(task: Task) -> Verification:
    """Check that the test command fails at base and passes at the solution commit."""
    v = Verification(task.id, None, None)
    try:
        base = gitutil.resolve_commit(task.repo, task.base)
        with gitutil.worktree(task.repo, base) as wt:
            apply_hidden_tests(task, wt)
            v.fails_at_base = not run_tests(task, wt).passed
        if task.solution:
            sol = gitutil.resolve_commit(task.repo, task.solution)
            with gitutil.worktree(task.repo, sol) as wt:
                apply_hidden_tests(task, wt)
                v.passes_at_solution = run_tests(task, wt).passed
    except (gitutil.GitError, OSError, ValueError) as exc:
        v.detail = str(exc)
    return v
