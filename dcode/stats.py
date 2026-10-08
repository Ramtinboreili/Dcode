"""Usage numbers: one line per finished task in ``~/.dcode/stats.jsonl``, summed on request.

Nothing leaves the machine. The log holds counts only (tokens, seconds, tool names, the model and
the project folder), never prompts, answers or file contents. Standard library only: the hub reads
it too.
"""

from __future__ import annotations

import json
import os
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import home_dir

MAX_LOG_BYTES = 4 * 1024 * 1024   # about 20,000 tasks; the older half is dropped beyond this
_lock = threading.Lock()


def stats_file() -> Path:
    return home_dir() / "stats.jsonl"


def record(entry: dict[str, Any]) -> None:
    """Append one finished task. Never raises: a full disk must not break a chat."""
    line = json.dumps({"t": datetime.now(timezone.utc).isoformat(timespec="seconds"), **entry},
                      ensure_ascii=False, separators=(",", ":"))
    path = stats_file()
    try:
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            if path.stat().st_size > MAX_LOG_BYTES:
                rows = path.read_text(encoding="utf-8", errors="replace").splitlines()
                path.write_text("\n".join(rows[len(rows) // 2:]) + "\n", encoding="utf-8")
    except OSError:
        pass


def load(days: int = 0) -> list[dict[str, Any]]:
    """Recorded tasks, oldest first; ``days`` > 0 keeps only the last that many days."""
    try:
        rows = stats_file().read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat() if days > 0 else ""
    out = []
    for row in rows:
        try:
            item = json.loads(row)
        except ValueError:
            continue  # a line cut off by a crash
        if isinstance(item, dict) and str(item.get("t", "")) >= since:
            out.append(item)
    return out


def _n(item: dict[str, Any], key: str) -> float:
    value = item.get(key)
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _group(items: list[dict[str, Any]], key: Any, top: int) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for item in items:
        name = key(item) or "—"
        row = groups.setdefault(name, {"name": name, "tasks": 0, "tokens_in": 0, "tokens_out": 0, "seconds": 0.0})
        row["tasks"] += 1
        row["tokens_in"] += int(_n(item, "prompt"))
        row["tokens_out"] += int(_n(item, "completion"))
        row["seconds"] = round(row["seconds"] + _n(item, "elapsed"), 1)
    ranked = sorted(groups.values(), key=lambda r: (r["tasks"], r["tokens_in"]), reverse=True)
    return ranked[:top]


def summarize(days: int = 30) -> dict[str, Any]:
    """Totals and breakdowns for the last ``days`` days (0 = everything recorded)."""
    items = load(days)
    tools: Counter[str] = Counter()
    for item in items:
        for name, count in (item.get("tools") or {}).items():
            if isinstance(count, int):
                tools[str(name)] += count
    per_day = {row["name"]: row for row in _group(items, lambda i: str(i.get("t", ""))[:10], 10_000)}
    span = days if days > 0 else 14
    today = datetime.now(timezone.utc).date()
    daily = [per_day.get(str(today - timedelta(days=back)),
                         {"name": str(today - timedelta(days=back)), "tasks": 0, "tokens_in": 0, "tokens_out": 0, "seconds": 0.0})
             for back in range(min(span, 14) - 1, -1, -1)]
    return {
        "days": days,
        "since": str(items[0].get("t", ""))[:10] if items else "",
        "tasks": len(items),
        "chats": len({(i.get("session"), i.get("chat")) for i in items}),
        "tokens_in": int(sum(_n(i, "prompt") for i in items)),
        "tokens_out": int(sum(_n(i, "completion") for i in items)),
        "seconds": round(sum(_n(i, "elapsed") for i in items), 1),
        "files_changed": int(sum(_n(i, "files") for i in items)),
        "commands": int(sum(_n(i, "commands") for i in items)),
        "helpers": int(sum(_n(i, "helpers") for i in items)),
        "tool_calls": sum(tools.values()),
        "models": _group(items, lambda i: str(i.get("model") or ""), 8),
        "projects": _group(items, lambda i: str(i.get("cwd") or ""), 8),
        "tools": [{"name": name, "calls": count} for name, count in tools.most_common(12)],
        "daily": daily,
    }


def human_tokens(count: int) -> str:
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 10_000:
        return f"{count / 1000:.0f}K"
    return f"{count / 1000:.1f}K" if count >= 1000 else str(count)


def human_time(seconds: float) -> str:
    seconds = int(seconds)
    if seconds >= 3600:
        return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"
    return f"{seconds // 60}m {seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


def render_text(summary: dict[str, Any]) -> str:
    """The summary as plain text (``dcode stats``; the terminal colors it itself)."""
    period = f"last {summary['days']} days" if summary["days"] else "all time"
    if not summary["tasks"]:
        return f"No tasks recorded yet ({period}). Numbers appear here after Dcode finishes its first task."
    lines = [
        f"Dcode usage — {period}" + (f" (first record {summary['since']})" if summary["since"] else ""),
        "",
        f"  Tasks          {summary['tasks']:,} in {summary['chats']:,} chat{'s' if summary['chats'] != 1 else ''}",
        f"  Tokens         {human_tokens(summary['tokens_in'])} in · {human_tokens(summary['tokens_out'])} out"
        + ("" if summary["tokens_in"] else "   (the model server reported none)"),
        f"  Working time   {human_time(summary['seconds'])}",
        f"  Tool calls     {summary['tool_calls']:,} · {summary['commands']:,} commands · "
        f"{summary['files_changed']:,} files changed · {summary['helpers']:,} helpers",
    ]

    def table(title: str, rows: list[dict[str, Any]], shorten: bool = False) -> None:
        if not rows:
            return
        lines.extend(["", f"  {title}"])
        for row in rows:
            name = row["name"]
            if shorten:
                home = os.path.expanduser("~")
                name = "~" + name[len(home):] if name.startswith(home) else name
            lines.append(f"    {name[:44]:<44} {row['tasks']:>5} tasks  {human_tokens(row['tokens_in']):>7} in  "
                         f"{human_tokens(row['tokens_out']):>7} out  {human_time(row['seconds']):>8}")

    table("By model", summary["models"])
    table("By project", summary["projects"], shorten=True)
    if summary["tools"]:
        lines.extend(["", "  Most used tools"])
        peak = summary["tools"][0]["calls"]
        for tool in summary["tools"]:
            bar = "█" * max(1, round(20 * tool["calls"] / peak))
            lines.append(f"    {tool['name'][:22]:<22} {tool['calls']:>6}  {bar}")
    active = [d for d in summary["daily"] if d["tasks"]]
    if active:
        lines.extend(["", "  Last days"])
        peak = max(d["tasks"] for d in summary["daily"])
        for day in summary["daily"]:
            bar = "█" * round(20 * day["tasks"] / peak) if day["tasks"] else ""
            lines.append(f"    {day['name']}  {day['tasks']:>4}  {bar}")
    return "\n".join(lines)
