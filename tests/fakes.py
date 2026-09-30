"""Fake runners for tests. None of them calls a real agent or API."""

from __future__ import annotations

import time

from conftest import CALC_FIXED

from taskreplay.runners.base import AgentResult, RunContext, Runner


class FixRunner(Runner):
    """Writes the correct fix, reports usage and cost like a real agent would."""

    type_name = "fake-fix"
    options = {"model", "cost"}

    def run(self, ctx: RunContext) -> AgentResult:
        (ctx.workdir / "calc.py").write_text(CALC_FIXED, encoding="utf-8")
        return AgentResult(
            tokens_in=1200, tokens_out=300, cost_usd=float(self.config.get("cost", 0.01)), turns=3,
            log="fixed calc.py",
        )


class NoopRunner(Runner):
    """Does nothing and reports tokens but no cost."""

    type_name = "fake-noop"
    options = {"model"}

    def run(self, ctx: RunContext) -> AgentResult:
        return AgentResult(tokens_in=500, tokens_out=20, turns=1)


class CheatRunner(Runner):
    """Fixes the bug but also edits a file outside allowed_files."""

    type_name = "fake-cheat"

    def run(self, ctx: RunContext) -> AgentResult:
        (ctx.workdir / "calc.py").write_text(CALC_FIXED, encoding="utf-8")
        (ctx.workdir / "README.md").write_text("changed\n", encoding="utf-8")
        return AgentResult(turns=1)


class DeleteTestRunner(Runner):
    """Tries to make the tests pass by deleting the failing test."""

    type_name = "fake-delete-test"

    def run(self, ctx: RunContext) -> AgentResult:
        (ctx.workdir / "tests" / "test_calc.py").write_text("def test_nothing():\n    pass\n", encoding="utf-8")
        return AgentResult(turns=1)


class CrashRunner(Runner):
    type_name = "fake-crash"

    def run(self, ctx: RunContext) -> AgentResult:
        raise RuntimeError("runner exploded")


class SlowRunner(Runner):
    type_name = "fake-slow"

    def run(self, ctx: RunContext) -> AgentResult:
        time.sleep(0.2)
        return AgentResult()
