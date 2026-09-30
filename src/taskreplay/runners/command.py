"""Generic runner: any shell command template, for agents without a built-in adapter."""

from __future__ import annotations

import json
import os
from pathlib import Path

from .. import procutil
from .base import AgentResult, RunContext, Runner, RunnerConfigError


class CommandRunner(Runner):
    """Options:
      command: shell command template (required). Placeholders, each quoted
        for the platform shell:
          {prompt_file}  file containing the prompt
          {workdir}      the task worktree (also the current directory)
          {usage_file}   optional JSON file the command may write with
                         {"tokens_in", "tokens_out", "cost_usd", "turns", "model"}
          {timeout}      agent timeout in seconds
          {task_id}, {attempt}
      stdin: true to also pipe the prompt on stdin (default false)
      env: mapping of extra environment variables
      model: label recorded in the results
    The same values are exported as TASKREPLAY_PROMPT_FILE, TASKREPLAY_WORKDIR,
    TASKREPLAY_USAGE_FILE, TASKREPLAY_TASK_ID and TASKREPLAY_ATTEMPT.
    """

    type_name = "command"
    options = {"command", "stdin", "env", "model"}

    def validate(self) -> None:
        if not isinstance(self.config.get("command"), str) or not self.config["command"].strip():
            raise RunnerConfigError(f"runner {self.name!r}: 'command' (string) is required")
        env = self.config.get("env", {})
        if not isinstance(env, dict):
            raise RunnerConfigError(f"runner {self.name!r}: env must be a mapping")

    def _paths(self, ctx: RunContext) -> tuple[Path, Path]:
        scratch = ctx.scratch_dir or ctx.workdir.parent
        return scratch / "prompt.txt", scratch / "usage.json"

    def render(self, ctx: RunContext) -> str:
        prompt_file, usage_file = self._paths(ctx)
        values = {
            "prompt_file": procutil.quote_arg(str(prompt_file)),
            "workdir": procutil.quote_arg(str(ctx.workdir)),
            "usage_file": procutil.quote_arg(str(usage_file)),
            "timeout": str(int(ctx.timeout)),
            "task_id": procutil.quote_arg(ctx.task_id),
            "attempt": str(ctx.attempt),
        }
        try:
            return self.config["command"].format(**values)
        except (KeyError, IndexError) as exc:
            raise RunnerConfigError(f"runner {self.name!r}: unknown placeholder {exc} in command") from None

    def describe(self, ctx: RunContext) -> str:
        return self.render(ctx)

    def run(self, ctx: RunContext) -> AgentResult:
        prompt_file, usage_file = self._paths(ctx)
        prompt_file.parent.mkdir(parents=True, exist_ok=True)
        prompt_file.write_text(ctx.prompt, encoding="utf-8")
        if usage_file.exists():
            usage_file.unlink()
        env = dict(os.environ)
        env.update({str(k): str(v) for k, v in (self.config.get("env") or {}).items()})
        env.update(
            {
                "TASKREPLAY_PROMPT_FILE": str(prompt_file),
                "TASKREPLAY_WORKDIR": str(ctx.workdir),
                "TASKREPLAY_USAGE_FILE": str(usage_file),
                "TASKREPLAY_TASK_ID": ctx.task_id,
                "TASKREPLAY_ATTEMPT": str(ctx.attempt),
            }
        )
        proc = procutil.run(
            self.render(ctx),
            cwd=ctx.workdir,
            timeout=ctx.timeout,
            input_text=ctx.prompt if self.config.get("stdin") else None,
            env=env,
            shell=True,
        )
        res = AgentResult(log=proc.stdout + ("\n--- stderr ---\n" + proc.stderr if proc.stderr else ""))
        if usage_file.exists():
            try:
                usage = json.loads(usage_file.read_text(encoding="utf-8"))
                for key in ("tokens_in", "tokens_out", "turns"):
                    if usage.get(key) is not None:
                        setattr(res, key, int(usage[key]))
                if usage.get("cost_usd") is not None:
                    res.cost_usd = float(usage["cost_usd"])
                    res.cost_source = "reported"
                if usage.get("model"):
                    res.model = str(usage["model"])
            except (ValueError, TypeError, AttributeError) as exc:
                res.extra["usage_file_error"] = str(exc)
        if proc.timed_out:
            res.status, res.error = "timeout", f"agent timed out after {ctx.timeout:.0f}s"
        elif proc.returncode != 0:
            res.status = "error"
            res.error = f"exit code {proc.returncode}: {proc.stderr.strip()[-300:]}"
        if not res.model and self.config.get("model"):
            res.model = str(self.config["model"])
        return res
