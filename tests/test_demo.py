"""The free demo in examples/ must keep working."""

import importlib.util
from pathlib import Path

from taskreplay.cli import main

DEMO = Path(__file__).resolve().parents[1] / "examples" / "demo.py"


def test_demo_end_to_end(tmp_path, capsys):
    spec = importlib.util.spec_from_file_location("taskreplay_demo", DEMO)
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    demo.main(str(tmp_path / "demo"))
    tasks = tmp_path / "demo" / "tasks.yaml"
    assert main(["validate", str(tasks)]) == 0
    out = tmp_path / "demo" / "results"
    assert main(["run", str(tasks), "--runners", "scripted-fixer,do-nothing", "--out", str(out)]) == 0
    assert main(["report", str(out)]) == 0
    text = capsys.readouterr().out
    assert "100% (1/1)" in text and "bugfix: scripted-fixer" in text
