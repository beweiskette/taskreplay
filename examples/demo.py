"""Build a tiny synthetic repository and a task file to try taskreplay for free.

    python examples/demo.py demo
    taskreplay validate demo/tasks.yaml
    taskreplay run demo/tasks.yaml --runners scripted-fixer,do-nothing --out demo/results
    taskreplay report demo/results

The two "agents" are local Python scripts, so no API is called and nothing
costs money. Swap in `claude`, `codex` or an openai-compatible runner once the
setup works.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

BUGGY = '''def slugify(text):
    """Lowercase, words joined by single dashes."""
    return text.lower().replace(" ", "-")


def clamp(value, low, high):
    return max(low, min(value, high))
'''

FIXED = '''import re


def slugify(text):
    """Lowercase, words joined by single dashes."""
    words = re.findall(r"[a-z0-9]+", text.lower())
    return "-".join(words)


def clamp(value, low, high):
    return max(low, min(value, high))
'''

TEST_BEFORE = '''from textutil import clamp


def test_clamp():
    assert clamp(5, 0, 3) == 3
'''

TEST_AFTER = TEST_BEFORE + '''

def test_slugify_collapses_spaces_and_punctuation():
    assert slugify("  Hello,   World! ") == "hello-world"
'''
TEST_AFTER = TEST_AFTER.replace("from textutil import clamp", "from textutil import clamp, slugify")

FIXER = f'''"""Stand-in agent: writes the known fix and reports made-up usage."""
import json, os
with open("textutil.py", "w", encoding="utf-8") as fh:
    fh.write({FIXED!r})
with open(os.environ["TASKREPLAY_USAGE_FILE"], "w") as fh:
    json.dump({{"tokens_in": 4200, "tokens_out": 350, "turns": 3}}, fh)
'''


def git(repo: Path, *args: str) -> str:
    env = dict(os.environ)
    env.update(
        GIT_AUTHOR_NAME="Demo", GIT_AUTHOR_EMAIL="demo@example.invalid",
        GIT_COMMITTER_NAME="Demo", GIT_COMMITTER_EMAIL="demo@example.invalid",
    )
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, stdout=subprocess.PIPE, text=True, env=env
    ).stdout.strip()


def main(target: str) -> None:
    root = Path(target).resolve()
    repo = root / "repo"
    if repo.exists():
        sys.exit(f"{repo} already exists")
    (repo / "tests").mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    (repo / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n", encoding="utf-8")
    (repo / "textutil.py").write_text(BUGGY, encoding="utf-8")
    (repo / "tests" / "test_textutil.py").write_text(TEST_BEFORE, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "initial version")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "textutil.py").write_text(FIXED, encoding="utf-8")
    (repo / "tests" / "test_textutil.py").write_text(TEST_AFTER, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "fix: slugify collapses spaces and drops punctuation")
    fix = git(repo, "rev-parse", "HEAD")

    (root / "fixer_agent.py").write_text(FIXER, encoding="utf-8")
    py = sys.executable.replace("\\", "/")
    fixer = (root / "fixer_agent.py").as_posix()
    (root / "tasks.yaml").write_text(
        f"""runners:
  scripted-fixer:
    type: command
    command: '"{py}" "{fixer}"'
    price_per_mtok_in: 3.0
    price_per_mtok_out: 15.0
  do-nothing:
    type: command
    command: '"{py}" -c "pass"'

tasks:
  - id: slugify-punctuation
    repo: repo
    base: {base}
    solution: {fix}
    prompt: |
      slugify("  Hello,   World! ") should return "hello-world": collapse runs of
      spaces and drop punctuation. Keep the function signature.
    test: '"{py}" -m pytest -q -p no:cacheprovider tests/test_textutil.py'
    allowed_files: [textutil.py]
    hidden_tests:
      - path: tests/test_textutil.py
        from_commit: {fix}
    tags: [bugfix]
    timeout: 300
""",
        encoding="utf-8",
    )
    print(f"demo repository: {repo}\ntask file:       {root / 'tasks.yaml'}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python examples/demo.py TARGET_DIR")
    main(sys.argv[1])
