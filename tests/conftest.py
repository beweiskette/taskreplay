"""Shared fixtures: a tiny synthetic git repository with a known bug fix."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

PY = sys.executable

CALC_BUGGY = '''def add(a, b):
    return a - b


def double(x):
    return x * 2
'''

CALC_FIXED = '''def add(a, b):
    return a + b


def double(x):
    return x * 2
'''

TEST_BEFORE = '''from calc import double


def test_double():
    assert double(3) == 6
'''

TEST_AFTER = '''from calc import add, double


def test_double():
    assert double(3) == 6


def test_add():
    assert add(2, 3) == 5
'''


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, stdout=subprocess.PIPE, text=True
    ).stdout.strip()


def quoted(path: str | Path) -> str:
    return f'"{path}"'


def toy_test_cmd() -> str:
    # `python -m pytest` puts the current directory (the worktree) on sys.path.
    return f"{quoted(PY)} -m pytest -q -p no:cacheprovider tests/test_calc.py"


@dataclass
class ToyRepo:
    path: Path
    base: str  # commit with the bug
    fix: str  # commit that fixes it and adds test_add
    docs: str  # later commit touching only docs
    noop: str  # commit touching code + tests whose tests already pass at parent


@pytest.fixture(autouse=True)
def git_identity(monkeypatch):
    for var, value in {
        "GIT_AUTHOR_NAME": "Test Author",
        "GIT_AUTHOR_EMAIL": "author@example.invalid",
        "GIT_COMMITTER_NAME": "Test Author",
        "GIT_COMMITTER_EMAIL": "author@example.invalid",
        "GIT_CONFIG_NOSYSTEM": "1",
    }.items():
        monkeypatch.setenv(var, value)


@pytest.fixture
def toy_repo(tmp_path: Path) -> ToyRepo:
    repo = tmp_path / "toy"
    (repo / "tests").mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "commit.gpgsign", "false")
    (repo / ".gitignore").write_text("__pycache__/\n*.pyc\n.pytest_cache/\n", encoding="utf-8")
    (repo / "calc.py").write_text(CALC_BUGGY, encoding="utf-8")
    (repo / "tests" / "test_calc.py").write_text(TEST_BEFORE, encoding="utf-8")
    (repo / "README.md").write_text("toy\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "initial")
    base = git(repo, "rev-parse", "HEAD")

    (repo / "calc.py").write_text(CALC_FIXED, encoding="utf-8")
    (repo / "tests" / "test_calc.py").write_text(TEST_AFTER, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "fix: add returns the sum\n\nadd() subtracted instead of adding.\n\nSigned-off-by: Test Author <author@example.invalid>")
    fix = git(repo, "rev-parse", "HEAD")

    (repo / "README.md").write_text("toy calculator\n", encoding="utf-8")
    git(repo, "commit", "-q", "-am", "docs: describe the project")
    docs = git(repo, "rev-parse", "HEAD")

    # Code + test change whose test already passes at the parent: not a valid task.
    (repo / "calc.py").write_text(CALC_FIXED + "\n\ndef triple(x):\n    return x * 3\n", encoding="utf-8")
    (repo / "tests" / "test_calc.py").write_text(TEST_AFTER + "\n\ndef test_double_zero():\n    assert double(0) == 0\n", encoding="utf-8")
    git(repo, "commit", "-q", "-am", "feat: add triple")
    noop = git(repo, "rev-parse", "HEAD")
    return ToyRepo(repo, base, fix, docs, noop)


def write_tasks(tmp_path: Path, toy: ToyRepo, extra: str = "", runners: str = "") -> Path:
    """A task file with one task: fix add() at the base commit."""
    text = f"""
{runners}
tasks:
  - id: fix-add
    repo: {toy.path.as_posix()}
    base: {toy.base}
    solution: {toy.fix}
    prompt: |
      add() in calc.py returns the wrong result. Make add(2, 3) return 5.
    test: '{toy_test_cmd()}'
    hidden_tests:
      - path: tests/test_calc.py
        from_commit: {toy.fix}
    tags: [bugfix]
    timeout: 60
{extra}
"""
    path = tmp_path / "tasks.yaml"
    path.write_text(text, encoding="utf-8")
    return path
