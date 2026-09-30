"""Subprocess helpers: run with a hard timeout and kill the whole process tree."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass

IS_WINDOWS = os.name == "nt"


@dataclass
class ProcResult:
    returncode: int | None  # None when the process was killed on timeout
    stdout: str
    stderr: str
    elapsed: float
    timed_out: bool


def _kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if IS_WINDOWS:
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def run(
    args: list[str] | str,
    *,
    cwd: str | os.PathLike | None = None,
    timeout: float | None = None,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
    shell: bool = False,
) -> ProcResult:
    """Run a command, capture text output, kill the process tree on timeout."""
    kwargs: dict = {}
    if IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    start = time.monotonic()
    proc = subprocess.Popen(
        args,
        cwd=cwd,
        env=env,
        shell=shell,
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **kwargs,
    )
    try:
        out, err = proc.communicate(input=input_text, timeout=timeout)
        return ProcResult(proc.returncode, out or "", err or "", time.monotonic() - start, False)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            out, err = "", ""
        return ProcResult(None, out or "", err or "", time.monotonic() - start, True)


def quote_arg(value: str) -> str:
    """Quote a single argument for the platform shell."""
    if IS_WINDOWS:
        return subprocess.list2cmdline([value])
    import shlex

    return shlex.quote(value)
