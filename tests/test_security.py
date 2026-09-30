"""Secrets must not reach the model, the logs or the results; API keys must not follow redirects.

Every secret-looking value here is synthetic and assembled at runtime so that
secret scanners do not flag this file.
"""

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from conftest import write_tasks
from test_openai_compat import fake_server, make_ctx, reply, tool_call  # noqa: F401

from taskreplay import engine, report
from taskreplay.runners.base import AgentResult, RunContext, Runner
from taskreplay.runners.openai_compat import OpenAICompatRunner, ToolError, Workspace
from taskreplay.tasks import load

PY = sys.executable

RUNNER_KEY = "synthetic-runner-key-" + "0123456789abcdef"
FAKE_OPENAI = "sk-" + "proj-" + "A1b2C3d4E5f6G7h8I9j0K1l2"
FAKE_GITHUB = "gh" + "p_" + "Z" * 36
FAKE_AWS = "AK" + "IA" + "ABCDEFGHIJKLMNOP"
FAKE_BEARER = "Bearer " + "abcDEF123456ghiJKL789"
FAKE_PEM = (
    "-----BEGIN " + "RSA PRIVATE KEY-----\n"
    "MIIEowIBAAKCAQEAsyntheticsyntheticsynthetic\n"
    "-----END " + "RSA PRIVATE KEY-----"
)


def _tool_messages(request):
    return [m["content"] for m in request["messages"] if m["role"] == "tool"]


def _runner(url, **extra):
    return OpenAICompatRunner("m", {"base_url": url, "model": "m", "api_key_env": "TR_TEST_RUNNER_KEY", **extra})


# -- run_command environment ---------------------------------------------------


def test_run_command_env_hides_runner_key_and_secret_vars(tmp_path, fake_server, monkeypatch):
    monkeypatch.setenv("TR_TEST_RUNNER_KEY", RUNNER_KEY)
    monkeypatch.setenv("OPENAI_API_KEY", "openai-value-" + "x" * 12)
    monkeypatch.setenv("SOME_SERVICE_TOKEN", "token-value-" + "y" * 12)
    monkeypatch.setenv("DB_PASSWORD", "password-value-" + "z" * 8)
    monkeypatch.setenv("TR_TEST_HARMLESS", "harmless-value")
    names = ["TR_TEST_RUNNER_KEY", "OPENAI_API_KEY", "SOME_SERVICE_TOKEN", "DB_PASSWORD", "TR_TEST_HARMLESS"]
    script = "import os; print({n: os.environ.get(n) for n in %r})" % (names,)
    fake_server["replies"] = [
        reply(tool_calls=[tool_call("c1", "run_command", command=f'"{PY}" -c "{script}"')]),
        reply("done"),
    ]
    res = _runner(fake_server["base_url"]).run(make_ctx(tmp_path))
    assert res.status == "ok", res.error
    out = _tool_messages(fake_server["requests"][1])[0]
    assert "'TR_TEST_HARMLESS': 'harmless-value'" in out  # ordinary variables still pass
    for name in names[:-1]:
        assert f"'{name}': None" in out, out
    for secret in (RUNNER_KEY, "openai-value", "token-value", "password-value"):
        assert secret not in out
        assert secret not in res.log


def test_child_env_filter():
    from taskreplay.redact import child_env, is_secret_env_name

    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY",
                 "db_password", "MY_SECRET", "CUSTOM_NAME"):
        assert is_secret_env_name(name) or name == "CUSTOM_NAME"
    for name in ("PATH", "HOME", "SYSTEMROOT", "PYTHONPATH", "LANG"):
        assert not is_secret_env_name(name)
    env = child_env({"PATH": "p", "CUSTOM_NAME": "v", "ANTHROPIC_API_KEY": "a"}, extra_names=["CUSTOM_NAME"])
    assert env == {"PATH": "p"}


# -- read_file -----------------------------------------------------------------


@pytest.mark.parametrize(
    "rel",
    [".env", ".env.local", "config/.env.production", "server.pem", "certs/tls.key", "id_rsa",
     ".ssh/id_ed25519", "secrets/store.p12", ".netrc", ".git-credentials", ".aws/credentials"],
)
def test_read_file_refuses_secret_files(tmp_path, rel):
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("SECRET_VALUE=do-not-show\n", encoding="utf-8")
    ws = Workspace(tmp_path)
    with pytest.raises(ToolError, match="secret"):
        ws.read_file(rel)


def test_read_file_allows_examples_and_configured_patterns(tmp_path):
    (tmp_path / ".env.example").write_text("API_KEY=\n", encoding="utf-8")
    (tmp_path / ".env").write_text("MODE=test\n", encoding="utf-8")
    (tmp_path / "id_rsa.pub").write_text("ssh-ed25519 AAAA test\n", encoding="utf-8")
    ws = Workspace(tmp_path)
    assert "API_KEY" in ws.read_file(".env.example")
    assert "ssh-ed25519" in ws.read_file("id_rsa.pub")
    with pytest.raises(ToolError):
        ws.read_file(".env")
    ws_allowed = Workspace(tmp_path, allow_secret_files=[".env"])
    assert "MODE=test" in ws_allowed.read_file(".env")


def _symlink(link, target, is_dir=False):
    try:
        os.symlink(target, link, target_is_directory=is_dir)
    except (OSError, NotImplementedError):
        pytest.skip("cannot create symlinks here")


def test_read_file_symlink_to_secret_is_refused(tmp_path):
    (tmp_path / ".env").write_text("SECRET_VALUE=do-not-show\n", encoding="utf-8")
    _symlink(tmp_path / "notes.txt", tmp_path / ".env")
    ws = Workspace(tmp_path)
    with pytest.raises(ToolError, match="secret"):
        ws.read_file("notes.txt")


def test_read_file_symlink_outside_worktree_is_refused(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    (tmp_path / "outside.txt").write_text("outside\n", encoding="utf-8")
    _symlink(wt / "link.txt", tmp_path / "outside.txt")
    ws = Workspace(wt)
    with pytest.raises(ToolError, match="escapes"):
        ws.read_file("link.txt")


def test_allow_secret_files_option_is_validated():
    from taskreplay.runners import RunnerConfigError

    OpenAICompatRunner("m", {"base_url": "http://127.0.0.1:1/v1", "model": "m", "allow_secret_files": [".env"]})
    with pytest.raises(RunnerConfigError):
        OpenAICompatRunner("m", {"base_url": "http://127.0.0.1:1/v1", "model": "m", "allow_secret_files": ".env"})


# -- redaction of tool output, logs and results ---------------------------------


def test_tool_output_and_log_are_redacted(tmp_path, fake_server, monkeypatch):
    monkeypatch.setenv("TR_TEST_RUNNER_KEY", RUNNER_KEY)
    leaky = "\n".join([
        f"openai = '{FAKE_OPENAI}'",
        f"github = '{FAKE_GITHUB}'",
        f"aws = '{FAKE_AWS}'",
        f"header = 'Authorization: {FAKE_BEARER}'",
        f"runner = '{RUNNER_KEY}'",
        FAKE_PEM,
        "ordinary = 'keep me'",
    ])
    fake_server["replies"] = [
        reply(tool_calls=[tool_call("c1", "write_file", path="settings.py", content=leaky)]),
        reply(tool_calls=[tool_call("c2", "read_file", path="settings.py")]),
        reply(tool_calls=[tool_call("c3", "run_command", command=f'"{PY}" -c "print(open(\'settings.py\').read())"')]),
        reply("done"),
    ]
    res = _runner(fake_server["base_url"]).run(make_ctx(tmp_path))
    assert res.status == "ok", res.error
    read_out = _tool_messages(fake_server["requests"][2])[-1]
    cmd_out = _tool_messages(fake_server["requests"][3])[-1]
    for out in (read_out, cmd_out, res.log):
        assert "keep me" in out
        assert "[REDACTED" in out
        for secret in (FAKE_OPENAI, FAKE_GITHUB, FAKE_AWS, "abcDEF123456ghiJKL789", RUNNER_KEY, "syntheticsynthetic"):
            assert secret not in out, (secret, out)


def test_redact_patterns():
    from taskreplay.redact import redact

    text = f"a {FAKE_OPENAI} b {FAKE_GITHUB} c {FAKE_AWS} d {FAKE_BEARER} e\n{FAKE_PEM}\nplain words stay"
    out = redact(text, ["custom-literal-value"])
    assert "plain words stay" in out
    for secret in (FAKE_OPENAI, FAKE_GITHUB, FAKE_AWS, "abcDEF123456ghiJKL789", "syntheticsynthetic"):
        assert secret not in out
    assert redact("x custom-literal-value y", ["custom-literal-value"]) == "x [REDACTED] y"
    assert redact("", ["v"]) == ""


class LeakyRunner(Runner):
    """Returns secrets in its log and error message, like a careless agent CLI."""

    type_name = "fake-leaky"

    def run(self, ctx: RunContext) -> AgentResult:
        return AgentResult(
            status="error",
            error=f"failed with key {FAKE_OPENAI}",
            log=f"env dump: TOKEN={FAKE_GITHUB}\n{FAKE_PEM}\n",
        )


def test_engine_redacts_results_logs_and_html(tmp_path, toy_repo):
    tf = load(write_tasks(tmp_path, toy_repo))
    runs = engine.plan(tf.tasks, [LeakyRunner("leaky", {})], 1)
    eng = engine.Engine(tmp_path / "results", log=lambda _: None)
    records = eng.run(runs)
    jsonl = eng.results_file.read_text(encoding="utf-8")
    log = (tmp_path / "results" / "logs" / f"{records[0]['run_id']}.log").read_text(encoding="utf-8")
    html = report.render_html([json.loads(line) for line in jsonl.splitlines()])
    for text in (jsonl, log, html):
        for secret in (FAKE_OPENAI, FAKE_GITHUB, "syntheticsynthetic"):
            assert secret not in text
    assert "[REDACTED" in jsonl and "[REDACTED" in log


def test_html_report_redacts_old_results():
    record = {"task_id": "t", "runner": "r", "attempt": 1, "passed": False, "status": "error",
              "error": f"leaked {FAKE_OPENAI}"}
    assert FAKE_OPENAI not in report.render_html([record])


# -- redirects -----------------------------------------------------------------


def _serve(handler_cls):
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_api_key_is_not_sent_across_redirects(tmp_path, monkeypatch):
    monkeypatch.setenv("TR_TEST_RUNNER_KEY", RUNNER_KEY)
    seen_by_second = []

    class Second(BaseHTTPRequestHandler):
        def _record(self):
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length:
                self.rfile.read(length)
            seen_by_second.append(dict(self.headers))
            body = json.dumps(reply("done")).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = _record

        def log_message(self, *args):
            pass

    second = _serve(Second)
    target = f"http://127.0.0.1:{second.server_address[1]}/v1/chat/completions"
    first_hits = []

    class First(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0) or 0)
            self.rfile.read(length)
            first_hits.append(self.headers.get("Authorization"))
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    first = _serve(First)
    try:
        url = f"http://127.0.0.1:{first.server_address[1]}/v1"
        res = _runner(url).run(make_ctx(tmp_path))
    finally:
        for s in (first, second):
            s.shutdown()
            s.server_close()
    assert first_hits == [f"Bearer {RUNNER_KEY}"]  # the configured endpoint gets the key
    assert seen_by_second == [], "the request must not be repeated at the redirect target"
    assert all(RUNNER_KEY not in json.dumps(h) for h in seen_by_second)
    assert res.status == "error"
    assert "redirect" in res.error.lower() and "302" in res.error
