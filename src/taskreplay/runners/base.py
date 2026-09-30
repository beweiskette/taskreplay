"""Runner interface shared by all agent adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar


class RunnerConfigError(ValueError):
    pass


@dataclass
class RunContext:
    """Everything a runner needs to attempt one task in one worktree."""

    workdir: Path
    prompt: str
    timeout: float
    task_id: str
    attempt: int
    budget_left: float | None = None  # USD, None when no --max-cost is set
    scratch_dir: Path | None = None  # outside the worktree, for prompt/usage files


@dataclass
class AgentResult:
    status: str = "ok"  # ok | timeout | error
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    cost_source: str | None = None  # "reported" | "estimated" | None
    turns: int | None = None
    model: str | None = None
    error: str | None = None
    log: str = ""  # raw agent output, saved next to the results
    extra: dict[str, Any] = field(default_factory=dict)


class Runner:
    """Base class. Subclasses set `type_name` and implement `run`.

    Options common to every runner:
      price_per_mtok_in / price_per_mtok_out: USD per million tokens, used to
        estimate cost when the agent does not report one itself.
    """

    type_name: ClassVar[str] = "base"
    #: Options this runner accepts in addition to the common ones.
    options: ClassVar[set[str]] = set()
    common_options: ClassVar[set[str]] = {"type", "price_per_mtok_in", "price_per_mtok_out"}

    def __init__(self, name: str, config: dict[str, Any] | None = None):
        self.name = name
        self.config = dict(config or {})
        unknown = set(self.config) - self.options - self.common_options
        if unknown:
            raise RunnerConfigError(
                f"runner {name!r} ({self.type_name}): unknown option(s): {', '.join(sorted(unknown))}"
            )
        self.validate()

    def validate(self) -> None:
        """Raise RunnerConfigError for bad configuration."""

    def describe(self, ctx: RunContext) -> str:
        """Human readable preview of what `run` would do (for --dry-run)."""
        return f"{self.type_name} runner"

    def run(self, ctx: RunContext) -> AgentResult:  # pragma: no cover - abstract
        raise NotImplementedError

    def estimate_cost(self, result: AgentResult) -> None:
        """Fill cost_usd from configured prices when the agent reported none."""
        if result.cost_usd is not None:
            if result.cost_source is None:
                result.cost_source = "reported"
            return
        p_in = self.config.get("price_per_mtok_in")
        p_out = self.config.get("price_per_mtok_out")
        if p_in is None and p_out is None:
            return
        if result.tokens_in is None and result.tokens_out is None:
            return
        cost = (result.tokens_in or 0) * float(p_in or 0) / 1e6
        cost += (result.tokens_out or 0) * float(p_out or 0) / 1e6
        result.cost_usd = round(cost, 6)
        result.cost_source = "estimated"
