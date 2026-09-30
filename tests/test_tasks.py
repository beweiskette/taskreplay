from pathlib import Path

import pytest

from taskreplay.tasks import HiddenTest, Task, TaskFileError, dump, parse


def test_parse_minimal_and_defaults(tmp_path):
    tf = parse(
        {"tasks": [{"id": "a", "base": "abc", "prompt": "do it", "test": "make test"}]},
        tmp_path,
    )
    t = tf.tasks[0]
    assert t.repo == tmp_path.resolve()
    assert t.timeout == 900 and t.test_timeout == 300
    assert t.full_prompt() == "do it\n"
    assert tf.runners == {}


def test_parse_list_form_and_runners(tmp_path):
    tf = parse([{"id": "a", "base": "abc", "prompt": "p", "test": "t", "repo": "sub"}], tmp_path)
    assert tf.tasks[0].repo == (tmp_path / "sub").resolve()
    tf = parse({"runners": {"x": {"type": "codex"}}, "tasks": []}, tmp_path)
    assert tf.runners == {"x": {"type": "codex"}}


@pytest.mark.parametrize(
    "task, message",
    [
        ({"base": "abc", "prompt": "p", "test": "t"}, "id"),
        ({"id": "a", "prompt": "p", "test": "t"}, "base"),
        ({"id": "a", "base": "b", "prompt": "p", "test": "t", "tset": "typo"}, "unknown field"),
        ({"id": "a", "base": "b", "prompt": "p", "test": "t", "hidden_tests": [{"path": "x"}]}, "exactly one"),
        ({"id": "a", "base": "b", "prompt": "p", "test": "t", "timeout": "soon"}, "number"),
    ],
)
def test_parse_errors(tmp_path, task, message):
    with pytest.raises(TaskFileError, match=message):
        parse({"tasks": [task]}, tmp_path)


def test_duplicate_ids(tmp_path):
    t = {"id": "a", "base": "b", "prompt": "p", "test": "t"}
    with pytest.raises(TaskFileError, match="duplicate"):
        parse({"tasks": [t, dict(t)]}, tmp_path)


def test_out_of_scope():
    t = Task(
        id="a", repo=Path("."), base="b", prompt="p", test="t",
        allowed_files=["src/*.py"], hidden_tests=[HiddenTest("tests/test_x.py", from_commit="c")],
    )
    assert t.out_of_scope(["src/a.py", "tests/test_x.py", "README.md"]) == ["README.md"]
    assert "src/*.py" in t.full_prompt()
    t.allowed_files = []
    assert t.out_of_scope(["anything"]) == []


def test_dump_roundtrip(tmp_path):
    repo = tmp_path / "repo"
    t = Task(
        id="a", repo=repo, base="b", prompt="line one\nline two\n", test="pytest -q",
        hidden_tests=[HiddenTest("tests/t.py", from_commit="c")], solution="c", tags=["bugfix"], timeout=120,
    )
    text = dump([t], tmp_path, "# header\n")
    assert text.startswith("# header\n")
    assert "prompt: |" in text  # multi-line prompts as literal blocks
    assert "repo: repo" in text
    back = parse(__import__("yaml").safe_load(text), tmp_path).tasks[0]
    assert back.prompt == t.prompt and back.repo == repo.resolve()
    assert back.hidden_tests[0].from_commit == "c" and back.timeout == 120 and back.tags == ["bugfix"]
