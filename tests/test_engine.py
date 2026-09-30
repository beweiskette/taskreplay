import json
from pathlib import Path

import fakes
import pytest
from conftest import CALC_BUGGY, git, write_tasks

from taskreplay import engine, gitutil
from taskreplay.runners import make_runner
from taskreplay.tasks import load


def _runner(cls, name=None, **config):
    return cls(name or cls.type_name, config)


def _run(tmp_path, toy, runners, attempts=1, extra="", **kwargs):
    tf = load(write_tasks(tmp_path, toy, extra=extra))
    runs = engine.plan(tf.tasks, runners, attempts)
    logs = []
    eng = engine.Engine(tmp_path / "results", log=logs.append, **kwargs)
    return eng, eng.run(runs), logs


def _worktrees(repo: Path) -> list[str]:
    return [ln for ln in git(repo, "worktree", "list", "--porcelain").splitlines() if ln.startswith("worktree ")]


def test_fix_passes_noop_fails(tmp_path, toy_repo):
    eng, records, _ = _run(tmp_path, toy_repo, [_runner(fakes.FixRunner), _runner(fakes.NoopRunner)])
    by = {r["runner"]: r for r in records}
    fix, noop = by["fake-fix"], by["fake-noop"]
    assert fix["passed"] is True and fix["tests_passed"] is True
    assert fix["tokens_in"] == 1200 and fix["tokens_out"] == 300
    assert fix["cost_usd"] == pytest.approx(0.01) and fix["cost_source"] == "reported"
    assert fix["turns"] == 3
    assert fix["changed_files"] == ["calc.py"]
    assert fix["diff_added"] == 1 and fix["diff_removed"] == 1
    assert noop["passed"] is False and noop["status"] == "ok"
    assert noop["diff_files"] == 0
    # results file, logs and patches exist
    lines = eng.results_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and json.loads(lines[0])["task_id"] == "fix-add"
    patch = (tmp_path / "results" / "patches" / f"{fix['run_id']}.patch").read_text(encoding="utf-8")
    assert "+    return a + b" in patch
    assert (tmp_path / "results" / "logs" / f"{fix['run_id']}.log").read_text(encoding="utf-8") == "fixed calc.py"


def test_main_checkout_untouched_and_worktrees_removed(tmp_path, toy_repo):
    head_before = git(toy_repo.path, "rev-parse", "HEAD")
    _run(tmp_path, toy_repo, [_runner(fakes.FixRunner), _runner(fakes.DeleteTestRunner)], attempts=2)
    assert git(toy_repo.path, "rev-parse", "HEAD") == head_before
    assert git(toy_repo.path, "status", "--porcelain") == ""
    assert len(_worktrees(toy_repo.path)) == 1  # only the main checkout
    # the main checkout still has the latest content, not the base content
    assert (toy_repo.path / "calc.py").read_text(encoding="utf-8") != CALC_BUGGY


def test_keep_leaves_worktree(tmp_path, toy_repo):
    _, records, _ = _run(tmp_path, toy_repo, [_runner(fakes.FixRunner)], keep=True)
    wt = Path(records[0]["worktree"])
    assert (wt / "calc.py").exists()
    assert len(_worktrees(toy_repo.path)) == 2
    gitutil.remove_worktree(toy_repo.path, wt)
    assert not wt.parent.exists()


def test_hidden_tests_override_agent_edits(tmp_path, toy_repo):
    _, records, _ = _run(tmp_path, toy_repo, [_runner(fakes.DeleteTestRunner)])
    rec = records[0]
    # The agent rewrote the test file, but scoring restores it from the solution commit.
    assert rec["changed_files"] == ["tests/test_calc.py"]
    assert rec["passed"] is False
    assert "test_add" in rec["test_output_tail"] or "assert" in rec["test_output_tail"]


def test_allowed_files_violation_fails(tmp_path, toy_repo):
    extra = "    allowed_files: ['calc.py']\n"
    # write_tasks appends extra after the task's keys, keep YAML indentation valid
    tasks_path = write_tasks(tmp_path, toy_repo)
    text = tasks_path.read_text(encoding="utf-8").replace("    timeout: 60\n", "    timeout: 60\n" + extra)
    tasks_path.write_text(text, encoding="utf-8")
    tf = load(tasks_path)
    assert "Only modify these files" in tf.tasks[0].full_prompt()
    eng = engine.Engine(tmp_path / "results", log=lambda _: None)
    records = eng.run(engine.plan(tf.tasks, [_runner(fakes.CheatRunner), _runner(fakes.FixRunner)], 1))
    by = {r["runner"]: r for r in records}
    assert by["fake-cheat"]["tests_passed"] is True
    assert by["fake-cheat"]["passed"] is False
    assert by["fake-cheat"]["out_of_scope_files"] == ["README.md"]
    assert by["fake-fix"]["passed"] is True


def test_crashing_runner_is_recorded(tmp_path, toy_repo):
    _, records, _ = _run(tmp_path, toy_repo, [_runner(fakes.CrashRunner), _runner(fakes.FixRunner)])
    crash = records[0]
    assert crash["status"] == "error" and "runner exploded" in crash["error"]
    assert crash["passed"] is False
    assert records[1]["passed"] is True


def test_max_cost_stops_new_runs(tmp_path, toy_repo):
    runners = [_runner(fakes.FixRunner, cost=0.6)]
    _, records, logs = _run(tmp_path, toy_repo, runners, attempts=3, max_cost=1.0)
    assert len(records) == 2  # 0.6 + 0.6 > 1.0 -> third run not started
    assert any("--max-cost reached" in line for line in logs)


def test_unknown_cost_does_not_count_but_is_noted(tmp_path, toy_repo):
    _, records, logs = _run(tmp_path, toy_repo, [_runner(fakes.NoopRunner)], attempts=2, max_cost=0.5)
    assert len(records) == 2
    assert any("reported no cost" in line for line in logs)


def test_price_config_estimates_cost(tmp_path, toy_repo):
    noop = fakes.NoopRunner("cheap", {"price_per_mtok_in": 1.0, "price_per_mtok_out": 2.0})
    _, records, _ = _run(tmp_path, toy_repo, [noop])
    assert records[0]["cost_usd"] == pytest.approx((500 * 1.0 + 20 * 2.0) / 1e6)
    assert records[0]["cost_source"] == "estimated"


def test_bad_base_commit(tmp_path, toy_repo):
    path = write_tasks(tmp_path, toy_repo)
    path.write_text(path.read_text(encoding="utf-8").replace(toy_repo.base, "deadbeef" * 5, 1), encoding="utf-8")
    tf = load(path)
    eng = engine.Engine(tmp_path / "results", log=lambda _: None)
    records = eng.run(engine.plan(tf.tasks, [_runner(fakes.FixRunner)], 1))
    assert records[0]["status"] == "error" and "base commit" in records[0]["error"]


def test_plan_order_and_max_tasks(tmp_path, toy_repo):
    tf = load(write_tasks(tmp_path, toy_repo))
    t = tf.tasks[0]
    a, b = _runner(fakes.FixRunner), _runner(fakes.NoopRunner)
    runs = engine.plan([t, t], [a, b], 2, max_tasks=1)
    assert [(r.runner.name, r.attempt) for r in runs] == [
        ("fake-fix", 1), ("fake-noop", 1), ("fake-fix", 2), ("fake-noop", 2)
    ]


def test_dotted_runner_type(tmp_path, toy_repo):
    runner = make_runner("mine", {"type": "fakes:FixRunner", "model": "x"})
    assert isinstance(runner, fakes.FixRunner)
