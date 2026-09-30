"""Codex CLI runner: `codex exec --json` inside the worktree."""

from __future__ import annotations

import json
import shutil
from typing import Any

from .. import procutil
from .base import AgentResult, RunContext, Runner, RunnerConfigError

# Item types that count as one agent step in the event stream.
STEP_ITEMS = {"command_execution", "file_change", "mcp_tool_call", "web_search", "agent_message"}


def parse_codex_events(stdout: str) -> AgentResult:
    """Sum token usage and count steps from `codex exec --json` JSONL events.

    Expected events (tolerant of unknown ones):
      {"type": "turn.completed", "usage": {"input_tokens": .., "cached_input_tokens": .., "output_tokens": ..}}
      {"type": "item.completed", "item": {"type": "command_execution", ...}}
      {"type": "turn.failed", "error": {"message": ..}} / {"type": "error", "message": ..}
    """
    res = AgentResult()
    tokens_in = tokens_out = cached = 0
    saw_usage = False
    steps = 0
    errors: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev: dict[str, Any] = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = ev.get("type")
        if etype == "turn.completed" and isinstance(ev.get("usage"), dict):
            u = ev["usage"]
            saw_usage = True
            tokens_in += int(u.get("input_tokens") or 0)
            cached += int(u.get("cached_input_tokens") or 0)
            tokens_out += int(u.get("output_tokens") or 0)
        elif etype == "item.completed" and isinstance(ev.get("item"), dict):
            if ev["item"].get("type") in STEP_ITEMS:
                steps += 1
        elif etype == "turn.failed":
            err = ev.get("error") or {}
            errors.append(str(err.get("message") if isinstance(err, dict) else err))
        elif etype == "error":
            errors.append(str(ev.get("message", "error")))
    if saw_usage:
        # input_tokens already includes cached input tokens in codex usage.
        res.tokens_in, res.tokens_out = tokens_in, tokens_out
        res.extra["cache_read_tokens"] = cached
    res.turns = steps
    if errors:
        res.status = "error"
        res.error = "; ".join(errors)[:500]
    return res


class CodexRunner(Runner):
    """Options:
      command: executable (default "codex")
      model: passed as --model
      sandbox: default "workspace-write" (writes limited to the worktree)
      extra_args: list of additional CLI arguments
    Codex does not report money cost; set price_per_mtok_in/out to estimate it.
    """

    type_name = "codex"
    options = {"command", "model", "sandbox", "extra_args"}

    def validate(self) -> None:
        v = self.config.get("extra_args", [])
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise RunnerConfigError(f"runner {self.name!r}: extra_args must be a list of strings")

    def build_args(self, ctx: RunContext) -> list[str]:
        args = [
            self.config.get("command", "codex"),
            "exec",
            "--json",
            "--sandbox",
            self.config.get("sandbox", "workspace-write"),
            "--cd",
            str(ctx.workdir),
            "--ephemeral",
            "--color",
            "never",
        ]
        if self.config.get("model"):
            args += ["--model", str(self.config["model"])]
        args += list(self.config.get("extra_args", []))
        args.append("-")  # read the prompt from stdin
        return args

    def describe(self, ctx: RunContext) -> str:
        return " ".join(self.build_args(ctx)) + "  < prompt on stdin"

    def run(self, ctx: RunContext) -> AgentResult:
        args = self.build_args(ctx)
        exe = shutil.which(args[0])
        if exe is None:
            return AgentResult(status="error", error=f"executable not found: {args[0]}")
        args[0] = exe
        proc = procutil.run(args, cwd=ctx.workdir, timeout=ctx.timeout, input_text=ctx.prompt)
        res = parse_codex_events(proc.stdout)
        res.log = proc.stdout + ("\n--- stderr ---\n" + proc.stderr if proc.stderr else "")
        if proc.timed_out:
            res.status, res.error = "timeout", f"agent timed out after {ctx.timeout:.0f}s"
        elif proc.returncode not in (0, None) and res.status == "ok":
            res.status = "error"
            res.error = f"exit code {proc.returncode}: {proc.stderr.strip()[:300]}"
        if self.config.get("model"):
            res.model = str(self.config["model"])
        return res
