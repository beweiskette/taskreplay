"""The built-in agent loop, driven by a local fake OpenAI-compatible server."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from conftest import CALC_BUGGY, CALC_FIXED

from taskreplay.runners.base import RunContext
from taskreplay.runners.openai_compat import OpenAICompatRunner, ToolError, Workspace


def tool_call(call_id, name, **arguments):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def reply(content=None, tool_calls=None, prompt_tokens=100, completion_tokens=20, **usage_extra):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    usage = {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, **usage_extra}
    return {"choices": [{"index": 0, "message": msg}], "usage": usage}


@pytest.fixture
def fake_server():
    """Serves scripted replies and records the requests it received."""
    state = {"replies": [], "requests": [], "headers": []}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            state["requests"].append(json.loads(self.rfile.read(length)))
            state["headers"].append(dict(self.headers))
            body = json.dumps(state["replies"].pop(0) if state["replies"] else reply("done")).encode()
            self.send_response(404 if "/wrong/" in self.path else 200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["base_url"] = f"http://127.0.0.1:{server.server_address[1]}/v1"
    yield state
    server.shutdown()
    server.server_close()


def make_ctx(tmp_path, name="wt", **kw):
    wt = tmp_path / name
    wt.mkdir()
    (wt / "calc.py").write_text(CALC_BUGGY, encoding="utf-8")
    args = dict(workdir=wt, prompt="Fix add() in calc.py.", timeout=60, task_id="t", attempt=1)
    args.update(kw)
    return RunContext(**args)


def test_agent_loop_fixes_file(tmp_path, fake_server, monkeypatch):
    monkeypatch.setenv("FAKE_KEY_FOR_TEST", "not-a-real-key")
    fake_server["replies"] = [
        reply(tool_calls=[tool_call("c1", "read_file", path="calc.py")]),
        reply(tool_calls=[tool_call("c2", "write_file", path="calc.py", content=CALC_FIXED)]),
        reply(tool_calls=[tool_call("c3", "run_command", command="python -c \"print('ran ok')\"")]),
        reply("Fixed add()."),
    ]
    runner = OpenAICompatRunner(
        "cheap",
        {
            "base_url": fake_server["base_url"],
            "model": "tiny-model",
            "api_key_env": "FAKE_KEY_FOR_TEST",
            "price_per_mtok_in": 1.0,
            "price_per_mtok_out": 2.0,
        },
    )
    ctx = make_ctx(tmp_path)
    res = runner.run(ctx)
    runner.estimate_cost(res)
    assert res.status == "ok", res.error
    assert (ctx.workdir / "calc.py").read_text(encoding="utf-8") == CALC_FIXED
    assert res.turns == 4
    assert (res.tokens_in, res.tokens_out) == (400, 80)
    assert res.cost_usd == pytest.approx((400 * 1 + 80 * 2) / 1e6)
    assert res.extra["stop_reason"] == "done"
    # the tool results were sent back to the model
    second = fake_server["requests"][1]["messages"]
    assert second[-1]["role"] == "tool" and "return a - b" in second[-1]["content"]
    fourth = fake_server["requests"][3]["messages"]
    assert "ran ok" in fourth[-1]["content"]
    assert fake_server["headers"][0]["Authorization"] == "Bearer not-a-real-key"
    assert fake_server["requests"][0]["model"] == "tiny-model"


def test_agent_loop_reports_gateway_cost(tmp_path, fake_server):
    fake_server["replies"] = [reply("nothing to do", cost=0.003)]
    runner = OpenAICompatRunner("gw", {"base_url": fake_server["base_url"], "model": "m"})
    res = runner.run(make_ctx(tmp_path))
    assert res.cost_usd == pytest.approx(0.003) and res.cost_source == "reported"
    assert "Authorization" not in fake_server["headers"][0]


def test_agent_loop_confines_paths(tmp_path, fake_server):
    outside = tmp_path / "outside.txt"
    fake_server["replies"] = [
        reply(tool_calls=[tool_call("c1", "write_file", path="../outside.txt", content="x")]),
        reply(tool_calls=[tool_call("c2", "read_file", path=str(outside))]),
        reply("gave up"),
    ]
    runner = OpenAICompatRunner("m", {"base_url": fake_server["base_url"], "model": "m"})
    res = runner.run(make_ctx(tmp_path))
    assert res.status == "ok"
    assert not outside.exists()
    msgs = fake_server["requests"][2]["messages"]
    tool_msgs = [m["content"] for m in msgs if m["role"] == "tool"]
    assert "escapes" in tool_msgs[0]
    assert "absolute" in tool_msgs[1]


def test_agent_loop_max_steps_and_budget(tmp_path, fake_server):
    fake_server["replies"] = [reply(tool_calls=[tool_call(f"c{i}", "read_file", path=".")]) for i in range(10)]
    runner = OpenAICompatRunner(
        "m",
        {"base_url": fake_server["base_url"], "model": "m", "max_steps": 3, "price_per_mtok_in": 1000.0},
    )
    res = runner.run(make_ctx(tmp_path))
    assert res.turns == 3 and res.extra["stop_reason"] == "max_steps"

    fake_server["requests"].clear()
    fake_server["replies"] = [reply(tool_calls=[tool_call(f"c{i}", "read_file", path=".")]) for i in range(10)]
    res = runner.run(make_ctx(tmp_path, name="second", budget_left=0.15))
    # each call costs 100 tokens * $1000/Mtok = $0.10, so the second call crosses $0.15
    assert res.turns == 2 and res.extra["stop_reason"] == "budget"


def test_agent_loop_missing_key(tmp_path, fake_server, monkeypatch):
    monkeypatch.delenv("UNSET_KEY_VAR_FOR_TEST", raising=False)
    runner = OpenAICompatRunner(
        "m", {"base_url": fake_server["base_url"], "model": "m", "api_key_env": "UNSET_KEY_VAR_FOR_TEST"}
    )
    res = runner.run(make_ctx(tmp_path))
    assert res.status == "error" and "UNSET_KEY_VAR_FOR_TEST" in res.error


def test_agent_loop_http_error(tmp_path, fake_server):
    runner = OpenAICompatRunner("m", {"base_url": fake_server["base_url"] + "/wrong", "model": "m"})
    res = runner.run(make_ctx(tmp_path))
    assert res.status == "error" and "HTTP 404" in res.error


def test_commands_can_be_disabled(tmp_path, fake_server):
    fake_server["replies"] = [reply(tool_calls=[tool_call("c1", "run_command", command="echo hi")]), reply("ok")]
    runner = OpenAICompatRunner("m", {"base_url": fake_server["base_url"], "model": "m", "allow_commands": False})
    runner.run(make_ctx(tmp_path))
    tool_names = [t["function"]["name"] for t in fake_server["requests"][0]["tools"]]
    assert "run_command" not in tool_names
    assert "disabled" in fake_server["requests"][1]["messages"][-1]["content"]


def test_workspace_rules(tmp_path):
    ws = Workspace(tmp_path, command_timeout=1, max_chars=50)
    (tmp_path / ".git").mkdir()
    for bad in ("../x", "/etc/passwd", ".git/config", "a/../../x", ""):
        with pytest.raises(ToolError):
            ws.resolve(bad)
    ws.write_file("sub/dir/f.txt", "hello")
    assert ws.read_file("sub/dir/f.txt") == "hello"
    assert "sub/" in ws.read_file(".")
    assert ".git" not in ws.read_file(".")
    ws.write_file("big.txt", "x" * 500)
    assert "truncated" in ws.read_file("big.txt")
    out = ws.run_command('python -c "import time; time.sleep(5)"')
    assert "timed out" in out


def test_config_validation():
    from taskreplay.runners import RunnerConfigError

    with pytest.raises(RunnerConfigError):
        OpenAICompatRunner("m", {"model": "m"})
    with pytest.raises(RunnerConfigError):
        OpenAICompatRunner("m", {"base_url": "http://127.0.0.1:1/v1"})
