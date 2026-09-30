import json
import sys
from pathlib import Path

import pytest

from taskreplay.runners import RunnerConfigError, make_runner, resolve_runners
from taskreplay.runners.base import AgentResult, RunContext
from taskreplay.runners.claude import ClaudeRunner, parse_claude_json, result_from_claude
from taskreplay.runners.codex import CodexRunner, parse_codex_events
from taskreplay.runners.command import CommandRunner

PY = sys.executable


def ctx(tmp_path, **kw):
    wt = tmp_path / "wt"
    wt.mkdir(exist_ok=True)
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    base = dict(workdir=wt, prompt="do the thing\n", timeout=30, task_id="t1", attempt=1, scratch_dir=scratch)
    base.update(kw)
    return RunContext(**base)


# -- claude --------------------------------------------------------------------

CLAUDE_OUTPUT = json.dumps(
    {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "duration_ms": 12000,
        "num_turns": 7,
        "result": "Done.",
        "total_cost_usd": 0.0421,
        "usage": {
            "input_tokens": 12,
            "cache_creation_input_tokens": 3000,
            "cache_read_input_tokens": 20000,
            "output_tokens": 800,
        },
        "modelUsage": {"model-a": {}},
    }
)


def test_claude_json_parsing():
    data = parse_claude_json("some banner\n" + CLAUDE_OUTPUT + "\n")
    res = result_from_claude(data)
    assert res.status == "ok"
    assert res.tokens_in == 23012 and res.tokens_out == 800
    assert res.cost_usd == pytest.approx(0.0421) and res.cost_source == "reported"
    assert res.turns == 7 and res.model == "model-a"


def test_claude_error_result():
    res = result_from_claude({"type": "result", "subtype": "error_max_turns", "is_error": True, "num_turns": 50})
    assert res.status == "error" and "max_turns" in res.error


def test_claude_args_are_least_permissive(tmp_path):
    r = ClaudeRunner("claude", {"model": "m1", "allowed_tools": ["Bash(python -m pytest:*)"]})
    args = r.build_args(ctx(tmp_path, budget_left=1.5))
    assert args[:2] == ["claude", "-p"]
    assert args[args.index("--output-format") + 1] == "json"
    assert args[args.index("--permission-mode") + 1] == "acceptEdits"
    assert "--dangerously-skip-permissions" not in args
    assert args[args.index("--max-budget-usd") + 1] == "1.5000"
    assert "Bash(python -m pytest:*)" in args
    assert "do the thing" not in " ".join(args)  # prompt goes via stdin


def test_claude_missing_executable(tmp_path):
    r = ClaudeRunner("claude", {"command": "definitely-not-an-installed-agent-xyz"})
    res = r.run(ctx(tmp_path))
    assert res.status == "error" and "not found" in res.error


def test_claude_runner_against_stub(tmp_path):
    # A stub script stands in for the claude executable.
    stub = tmp_path / "stub_claude.py"
    stub.write_text(
        "import sys\n"
        "prompt = sys.stdin.read()\n"
        "open('edited.txt', 'w').write(prompt)\n"
        f"print({CLAUDE_OUTPUT!r})\n",
        encoding="utf-8",
    )
    r = ClaudeRunner("claude", {"command": PY, "extra_args": [str(stub)]})
    # build_args puts claude flags first; for the stub we only need it to run.
    r.build_args = lambda c: [PY, str(stub)]
    c = ctx(tmp_path)
    res = r.run(c)
    assert res.status == "ok", res.error
    assert res.turns == 7
    assert (c.workdir / "edited.txt").read_text() == "do the thing\n"


# -- codex ---------------------------------------------------------------------

CODEX_EVENTS = "\n".join(
    json.dumps(e)
    for e in [
        {"type": "thread.started", "thread_id": "abc"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "1", "type": "reasoning", "text": "..."}},
        {"type": "item.completed", "item": {"id": "2", "type": "command_execution", "command": "ls"}},
        {"type": "item.completed", "item": {"id": "3", "type": "file_change", "changes": []}},
        {"type": "item.completed", "item": {"id": "4", "type": "agent_message", "text": "done"}},
        {"type": "turn.completed", "usage": {"input_tokens": 9000, "cached_input_tokens": 4000, "output_tokens": 700}},
    ]
)


def test_codex_event_parsing():
    res = parse_codex_events("warning: something\n" + CODEX_EVENTS)
    assert res.status == "ok"
    assert res.tokens_in == 9000 and res.tokens_out == 700
    assert res.extra["cache_read_tokens"] == 4000
    assert res.turns == 3  # reasoning items are not counted
    assert res.cost_usd is None


def test_codex_failed_turn():
    res = parse_codex_events(json.dumps({"type": "turn.failed", "error": {"message": "quota exceeded"}}))
    assert res.status == "error" and "quota" in res.error


def test_codex_args(tmp_path):
    r = CodexRunner("codex", {"model": "m2"})
    c = ctx(tmp_path)
    args = r.build_args(c)
    assert args[:3] == ["codex", "exec", "--json"]
    assert args[args.index("--sandbox") + 1] == "workspace-write"
    assert args[args.index("--cd") + 1] == str(c.workdir)
    assert "--dangerously-bypass-approvals-and-sandbox" not in args
    assert args[-1] == "-"


def test_codex_price_estimate():
    r = CodexRunner("codex", {"price_per_mtok_in": 2.0, "price_per_mtok_out": 8.0})
    res = parse_codex_events(CODEX_EVENTS)
    r.estimate_cost(res)
    assert res.cost_usd == pytest.approx(9000 * 2 / 1e6 + 700 * 8 / 1e6)
    assert res.cost_source == "estimated"


# -- command -------------------------------------------------------------------


def test_command_runner_usage_file(tmp_path):
    script = tmp_path / "agent.py"
    script.write_text(
        "import json, os, sys\n"
        "prompt = open(sys.argv[1], encoding='utf-8').read()\n"
        "open(os.path.join(sys.argv[2], 'out.txt'), 'w').write(prompt.upper())\n"
        "json.dump({'tokens_in': 10, 'tokens_out': 5, 'cost_usd': 0.002, 'turns': 2, 'model': 'tiny'},"
        " open(sys.argv[3], 'w'))\n",
        encoding="utf-8",
    )
    r = CommandRunner("mine", {"command": f'"{PY}" "{script}" {{prompt_file}} {{workdir}} {{usage_file}}'})
    c = ctx(tmp_path)
    res = r.run(c)
    assert res.status == "ok", res.error
    assert (c.workdir / "out.txt").read_text() == "DO THE THING\n"
    assert (res.tokens_in, res.tokens_out, res.turns, res.model) == (10, 5, 2, "tiny")
    assert res.cost_usd == pytest.approx(0.002)


def test_command_runner_env_and_stdin(tmp_path):
    script = tmp_path / "agent.py"
    script.write_text(
        "import os, sys\n"
        "data = sys.stdin.read()\n"
        "open('seen.txt', 'w').write(os.environ['TASKREPLAY_TASK_ID'] + '|' + os.environ['EXTRA'] + '|' + data)\n",
        encoding="utf-8",
    )
    r = CommandRunner("mine", {"command": f'"{PY}" "{script}"', "stdin": True, "env": {"EXTRA": "x"}})
    c = ctx(tmp_path)
    assert r.run(c).status == "ok"
    assert (c.workdir / "seen.txt").read_text() == "t1|x|do the thing\n"


def test_command_runner_timeout(tmp_path):
    r = CommandRunner("slow", {"command": f'"{PY}" -c "import time; time.sleep(30)"'})
    res = r.run(ctx(tmp_path, timeout=1))
    assert res.status == "timeout"


def test_command_runner_nonzero_exit(tmp_path):
    r = CommandRunner("bad", {"command": f'"{PY}" -c "import sys; sys.exit(3)"'})
    res = r.run(ctx(tmp_path))
    assert res.status == "error" and "exit code 3" in res.error


def test_command_runner_placeholder_quoting(tmp_path):
    spaced = tmp_path / "dir with space"
    spaced.mkdir()
    r = CommandRunner("q", {"command": "agent {workdir}"})
    rendered = r.render(ctx(tmp_path, workdir=spaced))
    assert str(spaced) in rendered
    assert rendered != f"agent {spaced}"  # quoted


def test_command_runner_requires_command():
    with pytest.raises(RunnerConfigError):
        CommandRunner("x", {})


# -- registry ------------------------------------------------------------------


def test_unknown_option_rejected():
    with pytest.raises(RunnerConfigError, match="unknown option"):
        make_runner("claude", {"permision_mode": "typo"})


def test_resolve_runners_errors():
    with pytest.raises(RunnerConfigError, match="not defined"):
        resolve_runners(["nope"], {})
    with pytest.raises(RunnerConfigError, match="unknown runner type"):
        resolve_runners(["x"], {"x": {"type": "nope"}})
    runners = resolve_runners(["claude", "fast"], {"fast": {"type": "codex", "model": "m"}})
    assert [r.type_name for r in runners] == ["claude", "codex"]


def test_estimate_cost_keeps_reported():
    r = make_runner("c", {"type": "codex", "price_per_mtok_in": 100})
    res = AgentResult(tokens_in=10, cost_usd=0.5)
    r.estimate_cost(res)
    assert res.cost_usd == 0.5 and res.cost_source == "reported"
