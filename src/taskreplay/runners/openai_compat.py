"""A minimal built-in agent loop for any OpenAI-compatible chat completions endpoint.

Three tools: read_file, write_file, run_command. File tools are confined to the
worktree. run_command runs with the worktree as current directory and a
timeout; it is not a sandbox (see README, Limitations). Set
`allow_commands: false` to disable it.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .. import procutil
from .base import AgentResult, RunContext, Runner, RunnerConfigError

SYSTEM_PROMPT = """You are a coding agent working in a git repository checked out in your current directory.
Complete the user's task by reading and editing files with the tools provided.
All paths are relative to the repository root. Keep changes minimal and focused.
When you are done, reply with a short summary and do not call any more tools."""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a text file, or list a directory, relative to the repository root.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Relative path; '.' lists the root."}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a text file relative to the repository root with the full new content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string", "description": "Complete new file content."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a shell command in the repository root (for example to run tests). Output is truncated.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
]


class ToolError(Exception):
    pass


class Workspace:
    """File and command access confined to one directory."""

    def __init__(self, root: Path, command_timeout: float = 60, max_chars: int = 20000, allow_commands: bool = True):
        self.root = Path(root).resolve()
        self.command_timeout = command_timeout
        self.max_chars = max_chars
        self.allow_commands = allow_commands

    def resolve(self, rel: str) -> Path:
        if not isinstance(rel, str) or not rel.strip():
            raise ToolError("path must be a non-empty string")
        candidate = Path(rel)
        if candidate.is_absolute() or candidate.drive or candidate.root:
            raise ToolError("absolute paths are not allowed; use a path relative to the repository root")
        path = (self.root / candidate).resolve()
        if path != self.root and self.root not in path.parents:
            raise ToolError("path escapes the repository root")
        rel_parts = path.relative_to(self.root).parts
        if rel_parts and rel_parts[0] == ".git":
            raise ToolError("access to .git is not allowed")
        return path

    def _truncate(self, text: str) -> str:
        if len(text) <= self.max_chars:
            return text
        half = self.max_chars // 2
        return text[:half] + f"\n... [{len(text) - self.max_chars} characters truncated] ...\n" + text[-half:]

    def read_file(self, path: str) -> str:
        p = self.resolve(path)
        if p.is_dir():
            entries = sorted(
                (e.name + ("/" if e.is_dir() else "")) for e in p.iterdir() if e.name != ".git"
            )
            return "\n".join(entries) or "(empty directory)"
        if not p.exists():
            raise ToolError(f"file not found: {path}")
        return self._truncate(p.read_text(encoding="utf-8", errors="replace"))

    def write_file(self, path: str, content: str) -> str:
        if not isinstance(content, str):
            raise ToolError("content must be a string")
        p = self.resolve(path)
        if p == self.root or p.is_dir():
            raise ToolError("path is a directory")
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
        return f"wrote {len(content)} characters to {path}"

    def run_command(self, command: str) -> str:
        if not self.allow_commands:
            raise ToolError("run_command is disabled for this runner")
        if not isinstance(command, str) or not command.strip():
            raise ToolError("command must be a non-empty string")
        proc = procutil.run(command, cwd=self.root, timeout=self.command_timeout, shell=True)
        if proc.timed_out:
            head = f"command timed out after {self.command_timeout:.0f}s"
        else:
            head = f"exit code {proc.returncode}"
        return self._truncate(f"{head}\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}")

    def call(self, name: str, arguments: dict[str, Any]) -> str:
        if name == "read_file":
            return self.read_file(arguments.get("path", ""))
        if name == "write_file":
            return self.write_file(arguments.get("path", ""), arguments.get("content"))
        if name == "run_command":
            return self.run_command(arguments.get("command", ""))
        raise ToolError(f"unknown tool: {name}")


class OpenAICompatRunner(Runner):
    """Options:
      base_url: e.g. https://api.example.com/v1 or http://localhost:1234/v1 (required)
      model: model name (required)
      api_key_env: name of the environment variable holding the API key
        (omit for local servers that need no key)
      max_steps: maximum model calls (default 40)
      command_timeout: seconds per run_command (default 60)
      allow_commands: default true
      request_timeout: seconds per HTTP request (default 180)
      temperature: optional
      max_output_chars: truncation limit for tool output (default 20000)
      system_prompt: optional replacement for the built-in system prompt
    """

    type_name = "openai-compatible"
    options = {
        "base_url", "model", "api_key_env", "max_steps", "command_timeout", "allow_commands",
        "request_timeout", "temperature", "max_output_chars", "system_prompt",
    }

    def validate(self) -> None:
        for key in ("base_url", "model"):
            if not self.config.get(key):
                raise RunnerConfigError(f"runner {self.name!r}: '{key}' is required")
        env_name = self.config.get("api_key_env")
        if env_name is not None and not isinstance(env_name, str):
            raise RunnerConfigError(f"runner {self.name!r}: api_key_env must be the NAME of an environment variable")

    def describe(self, ctx: RunContext) -> str:
        key = self.config.get("api_key_env")
        return (
            f"built-in agent loop -> {self.config['base_url']} model={self.config['model']}"
            f" key=${key or '(none)'} max_steps={self.config.get('max_steps', 40)}"
        )

    # -- HTTP -----------------------------------------------------------------

    def _post(self, body: dict[str, Any], timeout: float) -> dict[str, Any]:
        url = str(self.config["base_url"]).rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        env_name = self.config.get("api_key_env")
        if env_name:
            key = os.environ.get(env_name)
            if not key:
                raise RuntimeError(f"environment variable {env_name} is not set")
            headers["Authorization"] = f"Bearer {key}"
        data = json.dumps(body).encode("utf-8")
        last_error: Exception | None = None
        for delay in (0, 2, 5):
            if delay:
                time.sleep(delay)
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
                last_error = RuntimeError(f"HTTP {exc.code}: {detail}")
                if exc.code not in (429, 500, 502, 503, 504):
                    break
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                last_error = RuntimeError(f"request failed: {exc}")
        raise last_error or RuntimeError("request failed")

    # -- loop -----------------------------------------------------------------

    def run(self, ctx: RunContext) -> AgentResult:
        ws = Workspace(
            ctx.workdir,
            command_timeout=float(self.config.get("command_timeout", 60)),
            max_chars=int(self.config.get("max_output_chars", 20000)),
            allow_commands=bool(self.config.get("allow_commands", True)),
        )
        tools = TOOLS if ws.allow_commands else [t for t in TOOLS if t["function"]["name"] != "run_command"]
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.config.get("system_prompt") or SYSTEM_PROMPT},
            {"role": "user", "content": ctx.prompt},
        ]
        res = AgentResult(model=str(self.config["model"]), tokens_in=0, tokens_out=0, turns=0)
        reported_cost = 0.0
        saw_reported_cost = False
        transcript: list[dict[str, Any]] = []
        deadline = time.monotonic() + ctx.timeout
        max_steps = int(self.config.get("max_steps", 40))
        request_timeout = float(self.config.get("request_timeout", 180))
        stop_reason = "max_steps"
        try:
            for _ in range(max_steps):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    res.status, res.error = "timeout", f"agent timed out after {ctx.timeout:.0f}s"
                    stop_reason = "timeout"
                    break
                body: dict[str, Any] = {"model": self.config["model"], "messages": messages, "tools": tools}
                if self.config.get("temperature") is not None:
                    body["temperature"] = float(self.config["temperature"])
                reply = self._post(body, timeout=min(request_timeout, remaining))
                res.turns += 1
                usage = reply.get("usage") or {}
                res.tokens_in += int(usage.get("prompt_tokens") or 0)
                res.tokens_out += int(usage.get("completion_tokens") or 0)
                if usage.get("cost") is not None:  # some gateways report cost directly
                    reported_cost += float(usage["cost"])
                    saw_reported_cost = True
                choices = reply.get("choices") or []
                if not choices:
                    raise RuntimeError(f"response without choices: {str(reply)[:300]}")
                msg = choices[0].get("message") or {}
                tool_calls = msg.get("tool_calls") or []
                assistant: dict[str, Any] = {"role": "assistant", "content": msg.get("content") or ""}
                if tool_calls:
                    assistant["tool_calls"] = tool_calls
                messages.append(assistant)
                transcript.append({"assistant": msg.get("content"), "tool_calls": tool_calls})
                if not tool_calls:
                    stop_reason = "done"
                    break
                for call in tool_calls:
                    fn = call.get("function") or {}
                    name = fn.get("name", "")
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                        if not isinstance(args, dict):
                            raise ToolError("arguments must be a JSON object")
                        output = ws.call(name, args)
                    except json.JSONDecodeError:
                        output = "error: arguments are not valid JSON"
                    except ToolError as exc:
                        output = f"error: {exc}"
                    except OSError as exc:
                        output = f"error: {exc}"
                    messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": output})
                    transcript.append({"tool": name, "result": output[:2000]})
                if ctx.budget_left is not None:
                    probe = AgentResult(tokens_in=res.tokens_in, tokens_out=res.tokens_out)
                    self.estimate_cost(probe)
                    spent = reported_cost if saw_reported_cost else probe.cost_usd
                    if spent is not None and spent >= ctx.budget_left:
                        res.status, res.error = "error", "stopped: --max-cost budget reached"
                        stop_reason = "budget"
                        break
        except RuntimeError as exc:
            res.status, res.error = "error", str(exc)[:500]
            stop_reason = "error"
        if saw_reported_cost:
            res.cost_usd, res.cost_source = round(reported_cost, 6), "reported"
        res.extra["stop_reason"] = stop_reason
        res.log = json.dumps(transcript, indent=1, ensure_ascii=False)
        return res
