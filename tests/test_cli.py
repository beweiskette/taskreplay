"""End to end through the CLI with a `command` runner that is a local Python script."""

import json
import sys

from conftest import CALC_FIXED, git, write_tasks

from taskreplay.cli import main

PY = sys.executable


def fixer_runners(tmp_path) -> str:
    """A runners: block with a scripted 'agent' that fixes the bug, and one that does nothing."""
    script = tmp_path / "fixer.py"
    script.write_text(
        "import json, sys\n"
        f"open('calc.py', 'w', encoding='utf-8').write({CALC_FIXED!r})\n"
        "json.dump({'tokens_in': 900, 'tokens_out': 90, 'turns': 2}, open(sys.argv[1], 'w'))\n",
        encoding="utf-8",
    )
    fixer_cmd = f'"{PY}" "{script}" {{usage_file}}'
    idle_cmd = f'"{PY}" -c "pass"'
    return (
        "runners:\n"
        "  fixer:\n"
        "    type: command\n"
        f"    command: '{fixer_cmd}'\n"
        "    price_per_mtok_in: 3.0\n"
        "    price_per_mtok_out: 15.0\n"
        "  idle:\n"
        "    type: command\n"
        f"    command: '{idle_cmd}'\n"
    )


def test_dry_run_starts_nothing(tmp_path, toy_repo, capsys):
    tasks = write_tasks(tmp_path, toy_repo)
    out = tmp_path / "results"
    code = main(["run", str(tasks), "--runners", "claude,codex", "--attempts", "2", "--dry-run",
                 "--out", str(out), "--max-cost", "5"])
    assert code == 0
    text = capsys.readouterr().out
    assert "4 run(s) would be executed" in text
    assert "--permission-mode acceptEdits" in text
    assert "codex exec --json --sandbox workspace-write" in text
    assert "$5.00" in text
    assert not out.exists()
    assert len(git(toy_repo.path, "worktree", "list").splitlines()) == 1


def test_run_and_report_end_to_end(tmp_path, toy_repo, capsys):
    tasks = write_tasks(tmp_path, toy_repo, runners=fixer_runners(tmp_path))
    out = tmp_path / "results"
    assert main(["run", str(tasks), "--runners", "fixer,idle", "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "PASS" in printed and "FAIL" in printed
    files = list(out.glob("*.jsonl"))
    assert len(files) == 1
    records = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]
    by = {r["runner"]: r for r in records}
    assert by["fixer"]["passed"] is True
    assert by["fixer"]["cost_usd"] == (900 * 3.0 + 90 * 15.0) / 1e6
    assert by["fixer"]["cost_source"] == "estimated"
    assert by["idle"]["passed"] is False and by["idle"]["cost_usd"] is None

    html = tmp_path / "report.html"
    assert main(["report", str(out), "--html", str(html)]) == 0
    printed = capsys.readouterr().out
    assert "fixer" in printed and "100% (1/1)" in printed
    assert "bugfix: fixer" in printed
    page = html.read_text(encoding="utf-8")
    assert page.startswith("<!doctype html>") and "fixer" in page

    # default HTML location is inside the results directory
    assert main(["report", str(out)]) == 0
    assert (out / "report.html").exists()
    assert git(toy_repo.path, "status", "--porcelain") == ""


def test_run_errors(tmp_path, toy_repo, capsys):
    tasks = write_tasks(tmp_path, toy_repo)
    assert main(["run", str(tasks), "--runners", "unknown-runner"]) == 2
    assert "not defined" in capsys.readouterr().err
    assert main(["run", str(tasks), "--runners", "claude", "--only", "nope", "--dry-run"]) == 2
    bad = tmp_path / "bad.yaml"
    bad.write_text("tasks:\n  - id: x\n", encoding="utf-8")
    assert main(["run", str(bad), "--runners", "claude", "--dry-run"]) == 2


def test_extra_config_file(tmp_path, toy_repo, capsys):
    tasks = write_tasks(tmp_path, toy_repo)
    cfg = tmp_path / "runners.yaml"
    cfg.write_text("runners:\n  local:\n    type: openai-compatible\n    base_url: http://127.0.0.1:9/v1\n    model: small\n",
                   encoding="utf-8")
    assert main(["run", str(tasks), "--runners", "local", "--config", str(cfg), "--dry-run"]) == 0
    assert "model=small" in capsys.readouterr().out


def test_validate(tmp_path, toy_repo, capsys):
    tasks = write_tasks(tmp_path, toy_repo)
    assert main(["validate", str(tasks)]) == 0
    assert "ok  fix-add: fails at base; passes at solution" in capsys.readouterr().out
    # a task whose base already contains the fix is flagged
    text = tasks.read_text(encoding="utf-8").replace(f"base: {toy_repo.base}", f"base: {toy_repo.fix}")
    tasks.write_text(text, encoding="utf-8")
    assert main(["validate", str(tasks)]) == 1
    assert "PASSES at base" in capsys.readouterr().out


def test_report_missing(tmp_path, capsys):
    assert main(["report", str(tmp_path / "nothing")]) == 2
