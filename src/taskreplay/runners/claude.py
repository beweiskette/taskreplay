"""Claude Code runner: `claude -p --output-format json` inside the worktree."""

from __future__ import annotations

import json
import os
import shutil
from typing import Any

from .. import procutil
from .base import AgentResult, RunContext, Runner, RunnerConfigError


def parse_claude_json(stdout: str) -> dict[str, Any] | None:
    """Return the result object from `--output-format json` output."""
    text = stdout.strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    data = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue
    if isinstance(data, list):  # some versions print an event list
        results = [d for d in data if isinstance(d, dict) and d.get("type") == "result"]
        data = results[-1] if results else None
    return data if isinstance(data, dict) else None


def result_from_claude(data: dict[str, Any]) -> AgentResult:
    res = AgentResult()
    usage = data.get("usage") or {}
    if usage:
        res.tokens_in = int(usage.get("input_tokens") or 0) + int(
            usage.get("cache_creation_input_tokens") or 0
        ) + int(usage.get("cache_read_input_tokens") or 0)
        res.tokens_out = int(usage.get("output_tokens") or 0)
        res.extra["cache_read_tokens"] = int(usage.get("cache_read_input_tokens") or 0)
    if data.get("total_cost_usd") is not None:
        res.cost_usd = float(data["total_cost_usd"])
        res.cost_source = "reported"
    if data.get("num_turns") is not None:
        res.turns = int(data["num_turns"])
    models = data.get("modelUsage")
    if isinstance(models, dict) and models:
        res.model = ",".join(sorted(models))
    if data.get("is_error") or (data.get("subtype") not in (None, "success")):
        res.status = "error"
        res.error = str(data.get("subtype") or data.get("result") or "agent reported an error")[:500]
    return res


class ClaudeRunner(Runner):
    """Options:
      command: executable (default "claude")
      model: passed as --model
      permission_mode: default "acceptEdits" (edits inside the worktree are
        allowed, shell commands are denied unless listed in allowed_tools)
      allowed_tools: list passed to --allowedTools, e.g. ["Bash(python -m pytest:*)"]
      extra_args: list of additional CLI arguments
    """

    type_name = "claude"
    options = {"command", "model", "permission_mode", "allowed_tools", "extra_args"}

    def validate(self) -> None:
        for key in ("allowed_tools", "extra_args"):
            v = self.config.get(key, [])
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                raise RunnerConfigError(f"runner {self.name!r}: {key} must be a list of strings")

    def build_args(self, ctx: RunContext) -> list[str]:
        args = [
            self.config.get("command", "claude"),
            "-p",
            "--output-format",
            "json",
            "--permission-mode",
            self.config.get("permission_mode", "acceptEdits"),
            "--no-session-persistence",
        ]
        if self.config.get("model"):
            args += ["--model", str(self.config["model"])]
        if self.config.get("allowed_tools"):
            args += ["--allowedTools", *self.config["allowed_tools"]]
        if ctx.budget_left is not None:
            args += ["--max-budget-usd", f"{max(ctx.budget_left, 0.0):.4f}"]
        args += list(self.config.get("extra_args", []))
        return args

    def describe(self, ctx: RunContext) -> str:
        return " ".join(self.build_args(ctx)) + "  < prompt on stdin"

    def run(self, ctx: RunContext) -> AgentResult:
        args = self.build_args(ctx)
        exe = shutil.which(args[0])
        if exe is None:
            return AgentResult(status="error", error=f"executable not found: {args[0]}")
        args[0] = exe
        env = dict(os.environ)
        # Allow running taskreplay from inside a Claude Code session.
        env.pop("CLAUDECODE", None)
        proc = procutil.run(args, cwd=ctx.workdir, timeout=ctx.timeout, input_text=ctx.prompt, env=env)
        log = proc.stdout + ("\n--- stderr ---\n" + proc.stderr if proc.stderr else "")
        data = parse_claude_json(proc.stdout)
        res = result_from_claude(data) if data else AgentResult()
        res.log = log
        if proc.timed_out:
            res.status, res.error = "timeout", f"agent timed out after {ctx.timeout:.0f}s"
        elif data is None:
            res.status = "error"
            res.error = f"no JSON result (exit {proc.returncode}): {proc.stderr.strip()[:300]}"
        elif proc.returncode not in (0, None) and res.status == "ok":
            res.status, res.error = "error", f"exit code {proc.returncode}"
        if self.config.get("model") and not res.model:
            res.model = str(self.config["model"])
        return res
