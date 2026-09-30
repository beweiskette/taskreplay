"""Task file loading and saving (tasks.yaml)."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_TIMEOUT = 900
DEFAULT_TEST_TIMEOUT = 300


class TaskFileError(ValueError):
    pass


@dataclass
class HiddenTest:
    """A file placed into the worktree after the agent finishes, before scoring.

    Exactly one of `from_commit` (take the file from a commit of the task repo)
    or `from_file` (copy a local file, relative to the task file) is set.
    """

    path: str
    from_commit: str | None = None
    from_file: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"path": self.path}
        if self.from_commit:
            d["from_commit"] = self.from_commit
        if self.from_file:
            d["from_file"] = self.from_file
        return d


@dataclass
class Task:
    id: str
    repo: Path
    base: str
    prompt: str
    test: str
    allowed_files: list[str] = field(default_factory=list)
    timeout: float = DEFAULT_TIMEOUT
    test_timeout: float = DEFAULT_TEST_TIMEOUT
    hidden_tests: list[HiddenTest] = field(default_factory=list)
    solution: str | None = None
    tags: list[str] = field(default_factory=list)
    source_dir: Path = field(default_factory=Path.cwd)

    def full_prompt(self) -> str:
        """The prompt as sent to every runner (identical across runners)."""
        text = self.prompt.strip()
        if self.allowed_files:
            listed = "\n".join(f"- {p}" for p in self.allowed_files)
            text += f"\n\nOnly modify these files (glob patterns):\n{listed}"
        return text + "\n"

    def out_of_scope(self, changed: list[str]) -> list[str]:
        """Changed files that are not covered by allowed_files."""
        if not self.allowed_files:
            return []
        hidden = {h.path for h in self.hidden_tests}
        return [
            f
            for f in changed
            if f not in hidden and not any(fnmatch.fnmatchcase(f, pat) for pat in self.allowed_files)
        ]

    def to_dict(self, relative_to: Path | None = None) -> dict[str, Any]:
        repo = self.repo
        if relative_to is not None:
            try:
                repo = Path(_relpath(self.repo, relative_to))
            except ValueError:
                pass
        d: dict[str, Any] = {
            "id": self.id,
            "repo": repo.as_posix(),
            "base": self.base,
            "prompt": self.prompt,
            "test": self.test,
        }
        if self.solution:
            d["solution"] = self.solution
        if self.allowed_files:
            d["allowed_files"] = list(self.allowed_files)
        if self.hidden_tests:
            d["hidden_tests"] = [h.to_dict() for h in self.hidden_tests]
        if self.timeout != DEFAULT_TIMEOUT:
            d["timeout"] = self.timeout
        if self.test_timeout != DEFAULT_TEST_TIMEOUT:
            d["test_timeout"] = self.test_timeout
        if self.tags:
            d["tags"] = list(self.tags)
        return d


def _relpath(path: Path, start: Path) -> str:
    import os

    return os.path.relpath(path, start)


def _str_list(value: Any, what: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(value)
    raise TaskFileError(f"{what} must be a string or a list of strings")


def parse_task(raw: dict[str, Any], source_dir: Path) -> Task:
    if not isinstance(raw, dict):
        raise TaskFileError("each task must be a mapping")
    tid = raw.get("id")
    where = f"task {tid!r}" if tid else "task"
    for key in ("id", "base", "prompt", "test"):
        if not raw.get(key) or not isinstance(raw.get(key), (str, int)):
            raise TaskFileError(f"{where}: missing or invalid field '{key}'")
    known = {
        "id", "repo", "base", "prompt", "test", "allowed_files", "timeout",
        "test_timeout", "hidden_tests", "solution", "tags",
    }
    unknown = set(raw) - known
    if unknown:
        raise TaskFileError(f"{where}: unknown field(s): {', '.join(sorted(unknown))}")
    repo = Path(str(raw.get("repo", ".")))
    if not repo.is_absolute():
        repo = (source_dir / repo).resolve()
    hidden: list[HiddenTest] = []
    for h in raw.get("hidden_tests") or []:
        if not isinstance(h, dict) or not h.get("path"):
            raise TaskFileError(f"{where}: each hidden_tests entry needs 'path'")
        if bool(h.get("from_commit")) == bool(h.get("from_file")):
            raise TaskFileError(f"{where}: hidden test {h['path']!r} needs exactly one of from_commit / from_file")
        hidden.append(HiddenTest(path=str(h["path"]), from_commit=h.get("from_commit"), from_file=h.get("from_file")))
    try:
        timeout = float(raw.get("timeout", DEFAULT_TIMEOUT))
        test_timeout = float(raw.get("test_timeout", DEFAULT_TEST_TIMEOUT))
    except (TypeError, ValueError):
        raise TaskFileError(f"{where}: timeout values must be numbers") from None
    return Task(
        id=str(raw["id"]),
        repo=repo,
        base=str(raw["base"]),
        prompt=str(raw["prompt"]),
        test=str(raw["test"]),
        allowed_files=_str_list(raw.get("allowed_files"), f"{where}: allowed_files"),
        timeout=timeout,
        test_timeout=test_timeout,
        hidden_tests=hidden,
        solution=str(raw["solution"]) if raw.get("solution") else None,
        tags=_str_list(raw.get("tags"), f"{where}: tags"),
        source_dir=source_dir,
    )


@dataclass
class TaskFile:
    tasks: list[Task]
    runners: dict[str, dict[str, Any]]
    path: Path | None = None


def load(path: str | Path) -> TaskFile:
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise TaskFileError(f"{path}: invalid YAML: {exc}") from None
    return parse(data, path.resolve().parent, path)


def parse(data: Any, source_dir: Path, path: Path | None = None) -> TaskFile:
    if data is None:
        data = {}
    if isinstance(data, list):
        data = {"tasks": data}
    if not isinstance(data, dict):
        raise TaskFileError("task file must be a mapping with 'tasks:' (and optional 'runners:')")
    raw_tasks = data.get("tasks") or []
    if not isinstance(raw_tasks, list):
        raise TaskFileError("'tasks' must be a list")
    tasks = [parse_task(t, source_dir) for t in raw_tasks]
    seen: set[str] = set()
    for t in tasks:
        if t.id in seen:
            raise TaskFileError(f"duplicate task id {t.id!r}")
        seen.add(t.id)
    runners = data.get("runners") or {}
    if not isinstance(runners, dict) or not all(isinstance(v, dict) for v in runners.values()):
        raise TaskFileError("'runners' must be a mapping of name -> options")
    return TaskFile(tasks=tasks, runners=runners, path=path)


class _BlockDumper(yaml.SafeDumper):
    pass


def _str_representer(dumper: yaml.SafeDumper, value: str):
    # Multi-line strings (prompts) as literal blocks, easier to edit by hand.
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_BlockDumper.add_representer(str, _str_representer)


def dump(tasks: list[Task], relative_to: Path, header: str = "") -> str:
    body = yaml.dump(
        {"tasks": [t.to_dict(relative_to) for t in tasks]},
        Dumper=_BlockDumper,
        sort_keys=False,
        allow_unicode=True,
        width=100,
    )
    return header + body
