"""Runner registry.

A runner is selected by name. The name refers to an entry under `runners:` in
the task file (or a separate --config file). The built-in type names `claude`
and `codex` also work without any configuration.

Runner types: claude, codex, openai-compatible, command, or "package.module:Class"
for a custom subclass of `taskreplay.runners.base.Runner`.
"""

from __future__ import annotations

import importlib
from typing import Any

from .base import AgentResult, RunContext, Runner, RunnerConfigError
from .claude import ClaudeRunner
from .codex import CodexRunner
from .command import CommandRunner
from .openai_compat import OpenAICompatRunner

RUNNER_TYPES: dict[str, type[Runner]] = {
    ClaudeRunner.type_name: ClaudeRunner,
    CodexRunner.type_name: CodexRunner,
    OpenAICompatRunner.type_name: OpenAICompatRunner,
    CommandRunner.type_name: CommandRunner,
}

__all__ = [
    "AgentResult", "RunContext", "Runner", "RunnerConfigError", "RUNNER_TYPES",
    "register_runner", "make_runner", "resolve_runners",
]


def register_runner(cls: type[Runner]) -> type[Runner]:
    """Register a Runner subclass under its `type_name` (usable as a decorator)."""
    RUNNER_TYPES[cls.type_name] = cls
    return cls


def _load_type(type_name: str) -> type[Runner]:
    if type_name in RUNNER_TYPES:
        return RUNNER_TYPES[type_name]
    if ":" in type_name:
        module_name, _, attr = type_name.partition(":")
        try:
            cls = getattr(importlib.import_module(module_name), attr)
        except (ImportError, AttributeError) as exc:
            raise RunnerConfigError(f"cannot load runner type {type_name!r}: {exc}") from None
        if not (isinstance(cls, type) and issubclass(cls, Runner)):
            raise RunnerConfigError(f"{type_name!r} is not a Runner subclass")
        return cls
    known = ", ".join(sorted(RUNNER_TYPES))
    raise RunnerConfigError(f"unknown runner type {type_name!r} (known: {known})")


def make_runner(name: str, config: dict[str, Any] | None = None) -> Runner:
    config = dict(config or {})
    type_name = str(config.get("type") or name)
    return _load_type(type_name)(name, config)


def resolve_runners(names: list[str], configs: dict[str, dict[str, Any]]) -> list[Runner]:
    runners = []
    for name in names:
        if name not in configs and name not in RUNNER_TYPES:
            defined = ", ".join(sorted(configs)) or "none"
            raise RunnerConfigError(
                f"runner {name!r} is not defined under 'runners:' (defined: {defined}) and is not a built-in type"
            )
        runners.append(make_runner(name, configs.get(name)))
    return runners
