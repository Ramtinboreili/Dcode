#!/usr/bin/env python3
"""Turn a benchmark folder into report.md, CSVs and, if matplotlib is installed, plots.

    python bench/analyze.py --out results/a

Pass rates leave out `skipped` and `error` runs, which say nothing about an arm, and count `timeout` as a failure.
Confidence intervals are a cluster bootstrap over tasks: repeats of one task aren't independent, and the tasks are the
sample that a conclusion would generalise from. The same folder always gives the same report.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

RESAMPLES = 2000
COUNTED = ("pass", "fail", "timeout")
INTS = ("repeat", "order_index", "slot", "context_window", "check_exit_code", "exit_code", "steps", "calls", "estimated_calls", "tool_calls",
        "prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens", "compactions", "elided", "elision_count", "elision_regrets",
        "max_prompt_tokens", "diff_bytes")
FLOATS = ("wall_s", "model_s", "tool_s", "check_s")
MEASURES = ("prompt_tokens", "completion_tokens", "cached_tokens", "total_tokens", "calls", "steps", "wall_s", "model_s", "tool_s")
# The reference chart palette: one series on a light surface, with text in ink and never in the series colour.
SERIES, SURFACE, INK, MUTED, GRID = "#2a78d6", "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"


# --- reading ------------------------------------------------------------------------------------------------------
def _typed(row: dict[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = dict(row)
    for key in INTS:
        out[key] = int(row[key]) if row.get(key) not in (None, "") else None
    for key in FLOATS:
        out[key] = float(row[key]) if row.get(key) not in (None, "") else None
    return out


def read_rows(folder: Path) -> list[dict[str, Any]]:
    with open(folder / "results.csv", newline="", encoding="utf-8") as handle:
        return [_typed(row) for row in csv.DictReader(handle)]


def read_controls(folder: Path) -> dict[str, str]:
    """Each arm's comparison: its `control`, or baseline."""
    try:
        arms = json.loads((folder / "plan.json").read_text(encoding="utf-8"))["arms"]
    except (OSError, ValueError, KeyError):
        return {}
    return {arm["name"]: arm.get("control") or "baseline" for arm in arms}


def trace_path(folder: Path, row: dict[str, Any]) -> Path:
    return folder / "runs" / row["model"] / row["arm"] / row["task"] / f"r{row['repeat']}" / "trace.jsonl"


def llm_calls(path: Path) -> list[dict[str, Any]]:
    calls = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return calls
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("type") == "llm_call":
            calls.append(record)
    return calls


# --- statistics ---------------------------------------------------------------------------------------------------
def bootstrap_ci(clusters: dict[str, list[float]], label: str, resamples: int = RESAMPLES) -> tuple[float, float] | None:
    """A 95% interval for the mean: resample whole tasks. None with fewer than two tasks, where it would be a fiction."""
    groups = [(sum(values), len(values)) for _, values in sorted(clusters.items()) if values]
    if len(groups) < 2:
        return None
    rng = random.Random(label)  # by name, so an arm's interval doesn't depend on what else is in the folder
    means = []
    for _ in range(resamples):
        picked = rng.choices(groups, k=len(groups))
        means.append(sum(g[0] for g in picked) / sum(g[1] for g in picked))
    means.sort()
    return means[int(0.025 * resamples)], means[min(int(0.975 * resamples), resamples - 1)]


def mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def known(rows: list[dict[str, Any]], key: str) -> list[float]:
    return [row[key] for row in rows if row.get(key) is not None]


def arm_summary(rows: list[dict[str, Any]], resamples: int = RESAMPLES) -> list[dict[str, Any]]:
    groups: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(row["model"], row["arm"], row["context_window"])].append(row)
    out = []
    for (model, arm, window), members in groups.items():
        counted = [row for row in members if row["status"] in COUNTED]
        passed = [row for row in counted if row["status"] == "pass"]
        clusters: dict[str, list[float]] = defaultdict(list)
        for row in counted:
            clusters[row["task"]].append(1.0 if row["status"] == "pass" else 0.0)
        interval = bootstrap_ci(clusters, f"pass:{model}:{arm}:{window}", resamples)
        summary: dict[str, Any] = {"model": model, "arm": arm, "context_window": window, "runs": len(members), "counted": len(counted), "passed": len(passed),
                                   "timeouts": sum(r["status"] == "timeout" for r in members), "errors": sum(r["status"] == "error" for r in members),
                                   "skipped": sum(r["status"] == "skipped" for r in members), "pass_rate": len(passed) / len(counted) if counted else None,
                                   "pass_ci_low": interval[0] if interval else None, "pass_ci_high": interval[1] if interval else None}
        for key in MEASURES:
            values = known(counted, key)
            summary[f"mean_{key}"] = mean(values)
            summary[f"median_{key}"] = statistics.median(values) if values else None
        with_cache = [row for row in counted if row["cached_tokens"] is not None]
        prompts = sum(row["prompt_tokens"] or 0 for row in with_cache)
        summary["cached_ratio"] = sum(row["cached_tokens"] for row in with_cache) / prompts if prompts else None
        # What a success costs, the failures on the way to it included.
        summary["tokens_per_pass"] = sum(known(counted, "total_tokens")) / len(passed) if passed else None
        summary["end_reasons"] = dict(sorted(Counter(row["end_reason"] or "none" for row in counted).items()))
        out.append(summary)
    out.sort(key=lambda s: (s["model"], s["arm"] != "baseline", s["arm"], s["context_window"] or 0))
    return out


def paired(rows: list[dict[str, Any]], controls: dict[str, str], resamples: int = RESAMPLES) -> list[dict[str, Any]]:
    """Each arm minus its control, on the same model, task and repeat. A pair with a skipped or error side is left out."""
    table = {(r["model"], r["arm"], r["task"], r["repeat"]): r for r in rows if r["status"] in COUNTED}
    out = []
    arms = sorted({(r["model"], r["arm"]) for r in rows})
    for model, arm in arms:
        control = controls.get(arm, "baseline")
        if arm == control or (model, control) not in arms:
            continue
        passes: dict[str, list[float]] = defaultdict(list)
        tokens: dict[str, list[float]] = defaultdict(list)
        base_tokens: list[float] = []
        for (m, a, task, repeat), row in table.items():
            other = table.get((m, control, task, repeat))
            if m != model or a != arm or other is None:
                continue
            passes[task].append(float(row["status"] == "pass") - float(other["status"] == "pass"))
            tokens[task].append(float((row["total_tokens"] or 0) - (other["total_tokens"] or 0)))
            base_tokens.append(float(other["total_tokens"] or 0))
        pairs = sum(len(values) for values in passes.values())
        pass_ci = bootstrap_ci(passes, f"dpass:{model}:{arm}", resamples)
        token_ci = bootstrap_ci(tokens, f"dtok:{model}:{arm}", resamples)
        delta = mean([v for values in tokens.values() for v in values])
        base = mean(base_tokens)
        out.append({"model": model, "arm": arm, "control": control, "pairs": pairs, "tasks": len(passes),
                    "delta_pass": mean([v for values in passes.values() for v in values]), "delta_pass_low": pass_ci[0] if pass_ci else None, "delta_pass_high": pass_ci[1] if pass_ci else None,
                    "delta_tokens": delta, "delta_tokens_low": token_ci[0] if token_ci else None, "delta_tokens_high": token_ci[1] if token_ci else None,
                    "delta_tokens_pct": 100 * delta / base if delta is not None and base else None})
    return out


def prefix_summary(folder: Path, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """How much of each request a server's prefix cache could have reused, and what broke it when it couldn't."""
    groups: dict[tuple, dict[str, Any]] = defaultdict(lambda: {"ratios": [], "intact": 0, "broken": Counter(), "tools_changed": 0})
    for row in rows:
        if row["status"] not in COUNTED:
            continue
        group = groups[(row["model"], row["arm"])]
        for call in llm_calls(trace_path(folder, row)):
            if call.get("prev_messages") is None or not call.get("estimated_prompt_tokens"):
                continue  # the first call has nothing to share a prefix with
            group["ratios"].append(min(1.0, (call.get("lcp_tokens") or 0) / call["estimated_prompt_tokens"]))
            if call.get("tools_changed"):
                group["tools_changed"] += 1
            if call.get("prefix_intact"):
                group["intact"] += 1
            else:
                group["broken"][call.get("first_changed_role") or "got shorter"] += 1
    out = []
    for (model, arm), group in sorted(groups.items()):
        calls = len(group["ratios"])
        out.append({"model": model, "arm": arm, "calls": calls, "mean_lcp_ratio": mean(group["ratios"]), "intact_share": group["intact"] / calls if calls else None,
                    "tools_changed": group["tools_changed"], "breaks_by_role": dict(group["broken"].most_common())})
    return out


def regret_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A regret is the model asking again for an output that had been cut from its context."""
    groups: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row["status"] in COUNTED:
            groups[(row["model"], row["arm"])].append(row)
    out = []
    for (model, arm), members in sorted(groups.items()):
        regrets = sum(row["elision_regrets"] or 0 for row in members)
        stubbed = sum((row["elided"] or 0) + (row["elision_count"] or 0) for row in members)
        out.append({"model": model, "arm": arm, "runs": len(members), "stubbed_outputs": stubbed, "regrets": regrets, "regret_rate": regrets / stubbed if stubbed else None,
                    "regrets_per_run": regrets / len(members) if members else None, "runs_with_regret": sum(1 for row in members if row["elision_regrets"])})
    return out


def warnings(rows: list[dict[str, Any]], summary: list[dict[str, Any]]) -> list[str]:
    """What would change how the numbers should be read. Better said at the top of the report than found out later."""
    out = []
    by_model: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in summary:
        by_model[item["model"]].append(item)
    for model, items in by_model.items():
        rates = [(item["errors"] / item["runs"], item["arm"]) for item in items if item["runs"]]
        if rates and max(rates)[0] > 0:
            top = max(rates)
            spread = " Rates differ between arms, so leaving errors out may flatter some of them." if top[0] - min(rates)[0] > 0.05 else ""
            out.append(f"{model}: {sum(i['errors'] for i in items)} run(s) ended in `error` and aren't in the pass rates (most in `{top[1]}`, {top[0]:.0%}).{spread}")
    estimated = sum(row["estimated_calls"] or 0 for row in rows)
    if estimated:
        out.append(f"{estimated} model call(s) had no usage from the server, so their tokens are estimates.")
    commits = {row.get("dcode_commit") for row in rows if row["status"] != "skipped"}
    if len(commits) > 1:
        out.append(f"These runs came from {len(commits)} different Dcode commits, so differences may not be the arms' doing.")
    if any("dirty" in str(commit) for commit in commits):
        out.append("Dcode had uncommitted changes in `dcode/` during a run, so the commit doesn't fully identify the code.")
    leaked = sum(1 for row in rows if row.get("key_seen") == "True")
    if leaked:
        out.append(f"The API key appeared in the output of {leaked} run(s). It was redacted from their files, but the model saw it: replace the key.")
    if rows and all(row["cached_tokens"] is None for row in rows):
        out.append("The server reported no cached tokens, so the cached columns are empty. The prefix table comes from the requests and still holds.")
    return out


# --- writing ------------------------------------------------------------------------------------------------------
def _cell(value: Any) -> Any:
    return json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else ("" if value is None else value)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = list(rows[0]) if rows else []
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _cell(row.get(key)) for key in fields})


def _pct(value: float | None) -> str:
    return "–" if value is None else f"{value:.0%}"


def _range(low: float | None, high: float | None, signed: bool = False, percent: bool = True) -> str:
    if low is None or high is None:
        return "–"
    if percent:
        return f"{low * 100:+.0f} to {high * 100:+.0f}" if signed else f"{low:.0%}–{high:.0%}"
    return f"{low:+,.0f} to {high:+,.0f}"


def _num(value: float | None, digits: int = 0) -> str:
    return "–" if value is None else f"{value:,.{digits}f}"


def _table(head: list[str], rows: list[list[str]]) -> list[str]:
    if not rows:
        return ["(no data)", ""]
    numeric = ["|".join(["---"] * len(head))]
    return ["| " + " | ".join(head) + " |", "|" + numeric[0] + "|"] + ["| " + " | ".join(row) + " |" for row in rows] + [""]


def _reasons(counts: dict[str, int]) -> str:
    return ", ".join(f"{name} {count}" for name, count in counts.items()) or "–"


def build_report(rows: list[dict[str, Any]], summary: list[dict[str, Any]], pairs: list[dict[str, Any]], prefix: list[dict[str, Any]], regret: list[dict[str, Any]], note: str = "") -> str:
    statuses = Counter(row["status"] for row in rows)
    lines = ["# Benchmark report", ""]
    if note:
        lines += [f"> {note}", ""]
    lines += [f"{len(rows)} runs of {len({r['task'] for r in rows})} tasks, {len({r['arm'] for r in rows})} arms, {len({r['model'] for r in rows})} model(s): "
              + ", ".join(f"{statuses[s]} {s}" for s in ("pass", "fail", "timeout", "error", "skipped")) + ".", ""]
    notes = warnings(rows, summary)
    if notes:
        lines += ["## Read this first", ""] + [f"- {item}" for item in notes] + [""]

    lines += ["## Pass rate and cost", "", "`Counted` leaves out skipped and error runs. The interval is 95%, by resampling whole tasks. Token and time columns are means per counted run, with the median after the slash.", ""]
    lines += _table(["Model", "Arm", "Window", "Counted", "Pass rate", "95% CI", "Prompt tokens", "Completion", "Cached", "Cached share", "Calls", "Steps", "Wall s", "Model s", "Tool s", "Tokens per pass"],
                    [[s["model"], s["arm"], _num(s["context_window"]), str(s["counted"]), _pct(s["pass_rate"]), _range(s["pass_ci_low"], s["pass_ci_high"]),
                      f"{_num(s['mean_prompt_tokens'])} / {_num(s['median_prompt_tokens'])}", f"{_num(s['mean_completion_tokens'])} / {_num(s['median_completion_tokens'])}",
                      f"{_num(s['mean_cached_tokens'])} / {_num(s['median_cached_tokens'])}", _pct(s["cached_ratio"]), f"{_num(s['mean_calls'], 1)} / {_num(s['median_calls'], 1)}",
                      f"{_num(s['mean_steps'], 1)} / {_num(s['median_steps'], 1)}", f"{_num(s['mean_wall_s'], 1)} / {_num(s['median_wall_s'], 1)}",
                      f"{_num(s['mean_model_s'], 1)} / {_num(s['median_model_s'], 1)}", f"{_num(s['mean_tool_s'], 1)} / {_num(s['median_tool_s'], 1)}", _num(s["tokens_per_pass"])] for s in summary])
    lines += ["## How runs ended", ""]
    lines += _table(["Model", "Arm", "Window", "Timeouts", "Errors", "Skipped", "End reasons"],
                    [[s["model"], s["arm"], _num(s["context_window"]), str(s["timeouts"]), str(s["errors"]), str(s["skipped"]), _reasons(s["end_reasons"])] for s in summary])

    lines += ["## Against the control", "", "Each arm minus its control on the same task and repeat. An interval that includes 0 doesn't show a difference.", ""]
    lines += _table(["Model", "Arm", "Control", "Pairs", "Tasks", "Δ pass (points)", "95% CI", "Δ tokens", "Δ %", "95% CI"],
                    [[p["model"], p["arm"], p["control"], str(p["pairs"]), str(p["tasks"]), "–" if p["delta_pass"] is None else f"{p['delta_pass'] * 100:+.1f}",
                      _range(p["delta_pass_low"], p["delta_pass_high"], signed=True), "–" if p["delta_tokens"] is None else f"{p['delta_tokens']:+,.0f}",
                      "–" if p["delta_tokens_pct"] is None else f"{p['delta_tokens_pct']:+.0f}%", _range(p["delta_tokens_low"], p["delta_tokens_high"], percent=False)] for p in pairs])

    lines += ["## Prefix stability", "", "The share of each request that matches the one before it, as a one-slot prefix cache would see it. First calls aren't included. `Breaks` names the role of the first message that changed.", ""]
    lines += _table(["Model", "Arm", "Calls", "Mean LCP ratio", "Prefix intact", "Tools changed", "Breaks"],
                    [[p["model"], p["arm"], str(p["calls"]), _pct(p["mean_lcp_ratio"]), _pct(p["intact_share"]), str(p["tools_changed"]), _reasons(p["breaks_by_role"])] for p in prefix])

    lines += ["## Elision regret", "", "How often the model asked again for an output that had left its context. The rate is regrets per stubbed output.", ""]
    lines += _table(["Model", "Arm", "Runs", "Stubbed outputs", "Regrets", "Regret rate", "Per run", "Runs with a regret"],
                    [[r["model"], r["arm"], str(r["runs"]), str(r["stubbed_outputs"]), str(r["regrets"]), _pct(r["regret_rate"]), _num(r["regrets_per_run"], 2), str(r["runs_with_regret"])] for r in regret])

    categories = sorted({row["category"] for row in rows})
    per_arm: dict[tuple, list[str]] = defaultdict(list)
    for row in rows:
        if row["status"] in COUNTED:
            per_arm[(row["model"], row["arm"], row["category"])].append(row["status"])
    lines += ["## Pass rate by category", ""]
    lines += _table(["Model", "Arm", *categories],
                    [[model, arm, *[_pct(mean([float(s == "pass") for s in per_arm[(model, arm, c)]])) for c in categories]]
                     for model, arm in sorted({(r["model"], r["arm"]) for r in rows})])
    return "\n".join(lines).rstrip() + "\n"


def plots(folder: Path, summary: list[dict[str, Any]], pairs: list[dict[str, Any]]) -> list[Path]:
    """One dot-and-interval chart per model and question. Nothing here is needed for the report, whose tables hold the same numbers."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return []
    target = folder / "plots"
    target.mkdir(exist_ok=True)
    written = []

    def chart(path: Path, title: str, rows: list[tuple[str, float, float | None, float | None]], label: str, zero: bool) -> None:
        figure, axis = plt.subplots(figsize=(8, 0.42 * len(rows) + 1.5), facecolor=SURFACE)
        axis.set_facecolor(SURFACE)
        for y, (_, value, low, high) in enumerate(reversed(rows)):
            if low is not None and high is not None:
                axis.plot([low, high], [y, y], color=SERIES, linewidth=2, solid_capstyle="round", zorder=2)
            axis.plot([value], [y], "o", color=SERIES, markersize=8, markeredgecolor=SURFACE, markeredgewidth=2, zorder=3)
        axis.set_yticks(range(len(rows)), [row[0] for row in reversed(rows)], color=INK)
        axis.set_xlabel(label, color=MUTED)
        axis.set_title(title, color=INK, loc="left", fontsize=12)
        axis.tick_params(axis="x", colors=MUTED, length=0)
        axis.tick_params(axis="y", length=0)
        axis.grid(axis="x", color=GRID, linewidth=1)
        axis.set_axisbelow(True)
        if zero:
            axis.axvline(0, color=MUTED, linewidth=1, zorder=1)
        for side in axis.spines.values():
            side.set_visible(False)
        figure.tight_layout()
        figure.savefig(path, dpi=160, facecolor=SURFACE)
        plt.close(figure)
        written.append(path)

    for model in sorted({s["model"] for s in summary}):
        rates = [(f"{s['arm']} ({s['context_window']})" if s["context_window"] else s["arm"], s["pass_rate"] * 100,
                  None if s["pass_ci_low"] is None else s["pass_ci_low"] * 100, None if s["pass_ci_high"] is None else s["pass_ci_high"] * 100)
                 for s in summary if s["model"] == model and s["pass_rate"] is not None]
        if rates:
            chart(target / f"pass_rate_{model}.png", f"Pass rate by arm, {model}", rates, "Tasks passed (%), with the 95% interval", False)
        deltas = [(p["arm"], p["delta_tokens"], p["delta_tokens_low"], p["delta_tokens_high"]) for p in pairs if p["model"] == model and p["delta_tokens"] is not None]
        if deltas:
            chart(target / f"delta_tokens_{model}.png", f"Tokens per run against the control, {model}", deltas, "Difference in total tokens, with the 95% interval", True)
    return written


def analyze(folder: Path, note: str = "", resamples: int = RESAMPLES) -> str:
    rows = read_rows(folder)
    summary = arm_summary(rows, resamples)
    pairs = paired(rows, read_controls(folder), resamples)
    prefix = prefix_summary(folder, rows)
    regret = regret_summary(rows)
    write_csv(folder / "arm_summary.csv", summary)
    write_csv(folder / "paired.csv", pairs)
    write_csv(folder / "prefix.csv", prefix)
    write_csv(folder / "regret.csv", regret)
    report = build_report(rows, summary, pairs, prefix, regret, note)
    drawn = plots(folder, summary, pairs)
    report += "\n" + ("Plots: " + ", ".join(f"`plots/{path.name}`" for path in drawn) if drawn else "No plots: matplotlib isn't installed, or there was nothing to draw.") + "\n"
    (folder / "report.md").write_text(report, encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Analyze a benchmark folder.")
    parser.add_argument("--out", type=Path, required=True, help="the folder bench/run.py wrote")
    parser.add_argument("--note", default="", help="a line for the top of the report")
    parser.add_argument("--resamples", type=int, default=RESAMPLES)
    args = parser.parse_args(argv)
    if not (args.out / "results.csv").exists():
        print(f"analyze: no results.csv in {args.out}", file=sys.stderr)
        return 2
    analyze(args.out, args.note, args.resamples)
    print(args.out / "report.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
