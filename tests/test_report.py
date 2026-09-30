import json

import pytest

from taskreplay import report


def rec(runner, task, passed, time=10.0, tin=1000, tout=100, cost=None, tags=("bugfix",), status="ok", attempt=1):
    return {
        "runner": runner, "task_id": task, "passed": passed, "wall_time_s": time,
        "tokens_in": tin, "tokens_out": tout, "cost_usd": cost, "tags": list(tags),
        "status": status, "attempt": attempt, "turns": 4, "diff_added": 3, "diff_removed": 1, "diff_files": 1,
    }


RECORDS = [
    rec("alpha", "t1", True, 10, cost=0.10),
    rec("alpha", "t2", True, 30, cost=0.30),
    rec("alpha", "t3", False, 50, cost=0.20, tags=("feature",)),
    rec("beta", "t1", True, 5, cost=0.01),
    rec("beta", "t2", False, 6, cost=0.01, status="timeout"),
    rec("beta", "t3", True, 7, cost=0.01, tags=("feature",)),
    rec("gamma", "t1", False, 2, tin=None, tout=None, status="error"),
]


def test_runner_stats():
    stats = {s.runner: s for s in report.runner_stats(RECORDS)}
    a, b, g = stats["alpha"], stats["beta"], stats["gamma"]
    assert (a.runs, a.passed) == (3, 2)
    assert a.median_time == 30
    assert a.total_cost == pytest.approx(0.6)
    assert a.cost_per_solved == pytest.approx(0.3)
    assert b.cost_per_solved == pytest.approx(0.015)
    assert b.timeouts == 1 and g.errors == 1
    assert g.median_tokens is None and g.total_cost is None and g.cost_per_solved is None
    # sorted by pass rate, then cost per solved task
    assert [s.runner for s in report.runner_stats(RECORDS)] == ["beta", "alpha", "gamma"]


def test_recommendations_break_ties_by_cost():
    recs = report.recommendations(RECORDS)
    # bugfix: alpha 2/2... alpha t1,t2 pass (2/2 = 100%), beta 1/2, gamma 0/1
    assert recs["bugfix"] == "alpha"
    assert recs["feature"] == "beta"
    tie = [rec("x", "t", True, cost=1.0), rec("y", "t", True, cost=0.1)]
    assert report.recommendations(tie)["bugfix"] == "y"
    assert report.recommendations([rec("x", "t", False)])["bugfix"] == "no runner solved these tasks"


def test_text_and_matrices():
    text = report.render_text(RECORDS)
    assert "pass rate" in text and "cost/solved" in text
    assert "67% (2/3)" in text
    assert "Passed runs by tag" in text
    assert report.task_matrix(RECORDS)["t1"] == {"alpha": (1, 1), "beta": (1, 1), "gamma": (0, 1)}
    assert report.render_text([]) == "no results found"


def test_html_is_self_contained_and_escaped():
    evil = rec("<script>alert(1)</script>", "t<1>", True, cost=0.5)
    evil["error"] = "<b>boom</b>"
    page = report.render_html(RECORDS + [evil], title="My <report>")
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page and "&lt;b&gt;boom" in page
    assert "My &lt;report&gt;" in page
    assert "http://" not in page and "https://" not in page  # no external resources
    assert "prefers-color-scheme: dark" in page


def test_load_results_skips_garbage(tmp_path):
    f = tmp_path / "run-1.jsonl"
    f.write_text(json.dumps(RECORDS[0]) + "\nnot json\n\n" + json.dumps({"other": 1}) + "\n", encoding="utf-8")
    (tmp_path / "ignored.txt").write_text("x", encoding="utf-8")
    assert len(report.load_results([tmp_path])) == 1
    assert len(report.load_results([f])) == 1
