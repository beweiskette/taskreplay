"""Aggregate results/*.jsonl into a text table and a self-contained HTML report."""

from __future__ import annotations

import html
import json
import statistics
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


def load_results(paths: Iterable[str | Path]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for p in paths:
        p = Path(p)
        files = sorted(p.glob("*.jsonl")) if p.is_dir() else [p]
        for f in files:
            for line in f.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and "runner" in rec and "task_id" in rec:
                    records.append(rec)
    return records


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


@dataclass
class RunnerStats:
    runner: str
    runs: int
    passed: int
    errors: int
    timeouts: int
    median_time: float | None
    median_tokens: float | None
    median_turns: float | None
    median_diff_lines: float | None
    total_cost: float | None
    cost_runs: int  # runs with a known cost

    @property
    def pass_rate(self) -> float:
        return self.passed / self.runs if self.runs else 0.0

    @property
    def cost_per_solved(self) -> float | None:
        if self.total_cost is None or not self.passed:
            return None
        return self.total_cost / self.passed


def runner_stats(records: list[dict[str, Any]]) -> list[RunnerStats]:
    by_runner: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        by_runner.setdefault(r["runner"], []).append(r)
    stats = []
    for name, recs in by_runner.items():
        tokens = [
            (r.get("tokens_in") or 0) + (r.get("tokens_out") or 0)
            for r in recs
            if r.get("tokens_in") is not None or r.get("tokens_out") is not None
        ]
        costs = [float(r["cost_usd"]) for r in recs if r.get("cost_usd") is not None]
        stats.append(
            RunnerStats(
                runner=name,
                runs=len(recs),
                passed=sum(1 for r in recs if r.get("passed")),
                errors=sum(1 for r in recs if r.get("status") == "error"),
                timeouts=sum(1 for r in recs if r.get("status") == "timeout"),
                median_time=_median([float(r.get("wall_time_s") or 0) for r in recs]),
                median_tokens=_median(tokens),
                median_turns=_median([r["turns"] for r in recs if r.get("turns") is not None]),
                median_diff_lines=_median(
                    [(r.get("diff_added") or 0) + (r.get("diff_removed") or 0) for r in recs]
                ),
                total_cost=sum(costs) if costs else None,
                cost_runs=len(costs),
            )
        )
    stats.sort(key=lambda s: (-s.pass_rate, s.cost_per_solved if s.cost_per_solved is not None else float("inf"), s.runner))
    return stats


def tag_matrix(records: list[dict[str, Any]]) -> dict[str, dict[str, tuple[int, int]]]:
    """tag -> runner -> (passed, runs). Tasks without tags count as 'untagged'."""
    out: dict[str, dict[str, tuple[int, int]]] = {}
    for r in records:
        for tag in r.get("tags") or ["untagged"]:
            p, n = out.setdefault(tag, {}).get(r["runner"], (0, 0))
            out[tag][r["runner"]] = (p + (1 if r.get("passed") else 0), n + 1)
    return out


def task_matrix(records: list[dict[str, Any]]) -> dict[str, dict[str, tuple[int, int]]]:
    out: dict[str, dict[str, tuple[int, int]]] = {}
    for r in records:
        p, n = out.setdefault(r["task_id"], {}).get(r["runner"], (0, 0))
        out[r["task_id"]][r["runner"]] = (p + (1 if r.get("passed") else 0), n + 1)
    return out


def recommendations(records: list[dict[str, Any]]) -> dict[str, str]:
    """Per tag: the runner with the best pass rate; ties go to the cheaper
    (cost per solved task), then the faster one."""
    rec: dict[str, str] = {}
    by_tag: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        for tag in r.get("tags") or ["untagged"]:
            by_tag.setdefault(tag, []).append(r)
    for tag, recs in by_tag.items():
        stats = runner_stats(recs)
        best = stats[0] if stats else None
        if best is None or best.passed == 0:
            rec[tag] = "no runner solved these tasks"
            continue
        ties = [
            s for s in stats if s.pass_rate == best.pass_rate
        ]
        ties.sort(
            key=lambda s: (
                s.cost_per_solved if s.cost_per_solved is not None else float("inf"),
                s.median_time if s.median_time is not None else float("inf"),
            )
        )
        rec[tag] = ties[0].runner
    return rec


# -- formatting -----------------------------------------------------------------


def fmt_pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def fmt_time(x: float | None) -> str:
    return "-" if x is None else f"{x:.1f}s"


def fmt_num(x: float | None) -> str:
    if x is None:
        return "-"
    if x >= 10000:
        return f"{x / 1000:.0f}k"
    if x >= 1000:
        return f"{x / 1000:.1f}k"
    return f"{x:.0f}"


def fmt_cost(x: float | None) -> str:
    return "-" if x is None else f"${x:.4f}" if x < 1 else f"${x:.2f}"


SUMMARY_COLUMNS = [
    "runner", "runs", "pass rate", "median time", "median tokens", "median turns",
    "median diff", "total cost", "cost/solved", "errors", "timeouts",
]


def summary_rows(stats: list[RunnerStats]) -> list[list[str]]:
    rows = []
    for s in stats:
        cost = fmt_cost(s.total_cost)
        if s.total_cost is not None and s.cost_runs < s.runs:
            cost += f" ({s.cost_runs}/{s.runs} runs)"
        rows.append(
            [
                s.runner,
                str(s.runs),
                f"{fmt_pct(s.pass_rate)} ({s.passed}/{s.runs})",
                fmt_time(s.median_time),
                fmt_num(s.median_tokens),
                fmt_num(s.median_turns),
                fmt_num(s.median_diff_lines),
                cost,
                fmt_cost(s.cost_per_solved),
                str(s.errors),
                str(s.timeouts),
            ]
        )
    return rows


def text_table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        widths = [max(w, len(c)) for w, c in zip(widths, row)]
    line = "  ".join(h.ljust(w) for h, w in zip(headers, widths)).rstrip()
    sep = "  ".join("-" * w for w in widths)
    body = ["  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip() for row in rows]
    return "\n".join([line, sep, *body])


def render_text(records: list[dict[str, Any]]) -> str:
    if not records:
        return "no results found"
    stats = runner_stats(records)
    parts = [text_table(SUMMARY_COLUMNS, summary_rows(stats))]
    tags = tag_matrix(records)
    runners = [s.runner for s in stats]
    if len(tags) > 1 or "untagged" not in tags:
        rows = [
            [tag] + [f"{m[r][0]}/{m[r][1]}" if r in m else "-" for r in runners]
            for tag, m in sorted(tags.items())
        ]
        parts.append("\nPassed runs by tag:\n" + text_table(["tag", *runners], rows))
    recs = recommendations(records)
    parts.append("\nBest runner per tag (pass rate, then cost per solved task, then time):")
    parts += [f"  {tag}: {name}" for tag, name in sorted(recs.items())]
    return "\n".join(parts)


CSS = """
:root { --bg:#ffffff; --fg:#1d232b; --muted:#5d6874; --line:#d9dee4; --head:#f3f5f7;
        --pass:#1f7a3d; --passbg:#e3f3e8; --fail:#a3281f; --failbg:#f8e4e2; --part:#8a5a00; --partbg:#fbf0d9; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#14181d; --fg:#e4e8ec; --muted:#9aa5b1; --line:#2c333b; --head:#1c2229;
          --pass:#7fd49a; --passbg:#173524; --fail:#f09a91; --failbg:#3b1d1a; --part:#e8c16c; --partbg:#3a2e13; }
}
* { box-sizing:border-box; }
body { margin:0; padding:24px 16px 48px; background:var(--bg); color:var(--fg);
       font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }
main { max-width:1200px; margin:0 auto; }
h1 { font-size:22px; margin:0 0 4px; } h2 { font-size:16px; margin:32px 0 8px; }
p.meta { color:var(--muted); margin:0 0 16px; }
.scroll { overflow-x:auto; border:1px solid var(--line); border-radius:6px; }
table { border-collapse:collapse; width:100%; font-variant-numeric:tabular-nums; }
th, td { text-align:left; padding:6px 10px; border-bottom:1px solid var(--line); white-space:nowrap; }
th { background:var(--head); font-weight:600; }
tr:last-child td { border-bottom:none; }
td.pass { color:var(--pass); background:var(--passbg); }
td.fail { color:var(--fail); background:var(--failbg); }
td.part { color:var(--part); background:var(--partbg); }
td.wrap { white-space:normal; max-width:420px; color:var(--muted); }
ul { padding-left:20px; }
"""


def _cell_class(passed: int, runs: int) -> str:
    if runs == 0:
        return ""
    if passed == runs:
        return "pass"
    if passed == 0:
        return "fail"
    return "part"


def _table(headers: list[str], rows: list[list[str]], classes: list[list[str]] | None = None) -> str:
    e = html.escape
    head = "".join(f"<th>{e(h)}</th>" for h in headers)
    body = []
    for i, row in enumerate(rows):
        cells = []
        for j, cell in enumerate(row):
            cls = classes[i][j] if classes else ""
            cells.append(f'<td class="{cls}">{e(cell)}</td>' if cls else f"<td>{e(cell)}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'


def _matrix(matrix: dict[str, dict[str, tuple[int, int]]], runners: list[str], label: str) -> str:
    rows, classes = [], []
    for key, m in sorted(matrix.items()):
        row, cls = [key], [""]
        for r in runners:
            if r in m:
                row.append(f"{m[r][0]}/{m[r][1]}")
                cls.append(_cell_class(*m[r]))
            else:
                row.append("-")
                cls.append("")
        rows.append(row)
        classes.append(cls)
    return _table([label, *runners], rows, classes)


def render_html(records: list[dict[str, Any]], title: str = "taskreplay report") -> str:
    e = html.escape
    stats = runner_stats(records)
    runners = [s.runner for s in stats]
    tasks = {r["task_id"] for r in records}
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    recs = recommendations(records)
    run_rows, run_classes = [], []
    for r in sorted(records, key=lambda r: (r["task_id"], r["runner"], r.get("attempt", 0))):
        tokens = "-"
        if r.get("tokens_in") is not None or r.get("tokens_out") is not None:
            tokens = f"{fmt_num(r.get('tokens_in') or 0)} / {fmt_num(r.get('tokens_out') or 0)}"
        oos = r.get("out_of_scope_files") or []
        note = r.get("error") or ""
        if oos:
            note = (note + " " if note else "") + "out of scope: " + ", ".join(oos)
        run_rows.append(
            [
                r["task_id"],
                r["runner"],
                str(r.get("attempt", "")),
                "pass" if r.get("passed") else "fail",
                r.get("status", ""),
                fmt_time(r.get("wall_time_s")),
                tokens,
                fmt_cost(r.get("cost_usd")) + (" est." if r.get("cost_source") == "estimated" else ""),
                fmt_num(r.get("turns")),
                f"+{r.get('diff_added', 0)} -{r.get('diff_removed', 0)} ({r.get('diff_files', 0)} files)",
                note,
            ]
        )
        run_classes.append(["", "", "", "pass" if r.get("passed") else "fail", "", "", "", "", "", "", "wrap"])
    rec_items = "".join(f"<li><b>{e(tag)}</b>: {e(name)}</li>" for tag, name in sorted(recs.items()))
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)}</title>
<style>{CSS}</style>
</head>
<body>
<main>
<h1>{e(title)}</h1>
<p class="meta">{len(records)} runs, {len(tasks)} tasks, {len(runners)} runners. Generated {e(generated)}.
A run passes when the test command exits 0 and no file outside <code>allowed_files</code> was changed.</p>
<h2>Runners</h2>
{_table(SUMMARY_COLUMNS, summary_rows(stats))}
<h2>Best runner per tag</h2>
<p class="meta">Highest pass rate; ties go to the lower cost per solved task, then the lower median time.</p>
<ul>{rec_items}</ul>
<h2>Passed runs by tag</h2>
{_matrix(tag_matrix(records), runners, "tag")}
<h2>Passed runs by task</h2>
{_matrix(task_matrix(records), runners, "task")}
<h2>All runs</h2>
{_table(["task", "runner", "attempt", "result", "status", "time", "tokens in/out", "cost", "turns", "diff", "note"], run_rows, run_classes)}
</main>
</body>
</html>
"""
