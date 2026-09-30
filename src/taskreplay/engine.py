"""Orchestration: replay every (task, runner, attempt) in its own worktree."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import gitutil, scoring
from .runners import AgentResult, RunContext, Runner
from .tasks import Task


@dataclass
class PlannedRun:
    task: Task
    runner: Runner
    attempt: int


def plan(tasks: list[Task], runners: list[Runner], attempts: int, max_tasks: int | None = None) -> list[PlannedRun]:
    """Task-major order: every runner gets a task before the next task starts,
    so a budget stop leaves the runners with comparable coverage."""
    selected = tasks[:max_tasks] if max_tasks else tasks
    return [
        PlannedRun(task, runner, attempt)
        for task in selected
        for attempt in range(1, attempts + 1)
        for runner in runners
    ]


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name)[:80]


def describe_plan(runs: list[PlannedRun]) -> list[str]:
    lines = []
    for r in runs:
        ctx = RunContext(
            workdir=Path("<worktree>"),
            prompt=r.task.full_prompt(),
            timeout=r.task.timeout,
            task_id=r.task.id,
            attempt=r.attempt,
            scratch_dir=Path("<scratch>"),
        )
        lines.append(
            f"{r.task.id} | {r.runner.name} | attempt {r.attempt} | base {r.task.base} | "
            f"timeout {r.task.timeout:.0f}s\n    agent: {r.runner.describe(ctx)}\n    test:  {r.task.test}"
        )
    return lines


class Engine:
    def __init__(
        self,
        out_dir: Path,
        max_cost: float | None = None,
        keep: bool = False,
        log: Callable[[str], None] = print,
    ):
        self.out_dir = Path(out_dir)
        self.max_cost = max_cost
        self.keep = keep
        self.log = log
        self.spent = 0.0
        self.unknown_cost_runs = 0
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.results_file = self.out_dir / f"run-{stamp}.jsonl"
        self.session = stamp

    def budget_left(self) -> float | None:
        return None if self.max_cost is None else self.max_cost - self.spent

    def run(self, runs: list[PlannedRun]) -> list[dict[str, Any]]:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        (self.out_dir / "logs").mkdir(exist_ok=True)
        (self.out_dir / "patches").mkdir(exist_ok=True)
        records = []
        for i, planned in enumerate(runs, 1):
            left = self.budget_left()
            if left is not None and left <= 0:
                self.log(f"--max-cost reached (${self.spent:.4f} spent); skipping {len(runs) - i + 1} remaining run(s)")
                break
            self.log(f"[{i}/{len(runs)}] {planned.task.id} with {planned.runner.name} (attempt {planned.attempt})")
            record = self.run_one(planned)
            records.append(record)
            with open(self.results_file, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            cost = record.get("cost_usd")
            if cost is not None:
                self.spent += float(cost)
            else:
                self.unknown_cost_runs += 1
            verdict = "PASS" if record["passed"] else "FAIL"
            cost_txt = f"${cost:.4f}" if cost is not None else "cost n/a"
            tin, tout = record.get("tokens_in"), record.get("tokens_out")
            tokens_txt = "tokens n/a" if tin is None and tout is None else f"tokens={tin or 0}/{tout or 0}"
            self.log(
                f"    {verdict}  status={record['status']}  {record['wall_time_s']:.1f}s  "
                f"{tokens_txt}  {cost_txt}"
                + (f"  error={record['error']}" if record.get("error") else "")
            )
        if self.max_cost is not None and self.unknown_cost_runs:
            self.log(
                f"note: {self.unknown_cost_runs} run(s) reported no cost; they do not count toward --max-cost"
            )
        return records

    def run_one(self, planned: PlannedRun) -> dict[str, Any]:
        task, runner, attempt = planned.task, planned.runner, planned.attempt
        run_id = f"{self.session}_{_safe(task.id)}_{_safe(runner.name)}_a{attempt}"
        record: dict[str, Any] = {
            "run_id": run_id,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "task_id": task.id,
            "tags": list(task.tags),
            "runner": runner.name,
            "runner_type": runner.type_name,
            "model": runner.config.get("model"),
            "attempt": attempt,
            "status": "error",
            "passed": False,
            "wall_time_s": 0.0,
            "tokens_in": None,
            "tokens_out": None,
            "cost_usd": None,
            "cost_source": None,
            "turns": None,
            "diff_files": 0,
            "diff_added": 0,
            "diff_removed": 0,
            "changed_files": [],
            "out_of_scope_files": [],
            "tests_passed": False,
            "test_exit_code": None,
            "test_time_s": None,
            "test_output_tail": "",
            "error": None,
        }
        try:
            base = gitutil.resolve_commit(task.repo, task.base)
        except (gitutil.GitError, OSError) as exc:
            record["error"] = f"cannot resolve base commit: {exc}"
            return record
        record["base_commit"] = base
        wt = gitutil.add_worktree(task.repo, base)
        try:
            scratch = wt.parent / "scratch"
            scratch.mkdir(exist_ok=True)
            ctx = RunContext(
                workdir=wt,
                prompt=task.full_prompt(),
                timeout=task.timeout,
                task_id=task.id,
                attempt=attempt,
                budget_left=self.budget_left(),
                scratch_dir=scratch,
            )
            start = time.monotonic()
            try:
                result = runner.run(ctx)
            except Exception as exc:  # a broken runner must not stop the benchmark
                result = AgentResult(status="error", error=f"{type(exc).__name__}: {exc}")
            record["wall_time_s"] = round(time.monotonic() - start, 3)
            runner.estimate_cost(result)
            record.update(
                status=result.status,
                tokens_in=result.tokens_in,
                tokens_out=result.tokens_out,
                cost_usd=result.cost_usd,
                cost_source=result.cost_source,
                turns=result.turns,
                error=result.error,
            )
            if result.model:
                record["model"] = result.model
            if result.extra:
                record["extra"] = result.extra
            (self.out_dir / "logs" / f"{run_id}.log").write_text(result.log or "", encoding="utf-8")

            diff = gitutil.diff_against(wt, base)
            record.update(
                diff_files=len(diff.files),
                diff_added=diff.added,
                diff_removed=diff.removed,
                changed_files=diff.files,
                out_of_scope_files=task.out_of_scope(diff.files),
            )
            (self.out_dir / "patches" / f"{run_id}.patch").write_text(diff.patch, encoding="utf-8")

            scoring.apply_hidden_tests(task, wt)
            outcome = scoring.run_tests(task, wt)
            record.update(
                tests_passed=outcome.passed,
                test_exit_code=outcome.exit_code,
                test_time_s=outcome.elapsed,
                test_output_tail=outcome.output_tail,
            )
            record["passed"] = outcome.passed and not record["out_of_scope_files"]
        except (gitutil.GitError, OSError, ValueError) as exc:
            record["error"] = (record.get("error") or "") + f" scoring failed: {exc}"
        finally:
            if self.keep:
                record["worktree"] = str(wt)
                self.log(f"    kept worktree: {wt}")
            else:
                gitutil.remove_worktree(task.repo, wt)
        return record
