"""What the admin's monitoring page shows: usage, each model's latency and speed, errors, and live health.

Everything except live health comes from the usage table (one row per request). Live health asks each
model's server for its model list and, when it has a metrics URL (vLLM's ``/metrics``), reads the
running/waiting requests and KV-cache use from it.
"""

from __future__ import annotations

import math
import re
import time
import urllib.request
from typing import Any

from .discovery import probe
from .store import Store

RANGES = {"1h": 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}
BUCKETS = 48
VLLM_METRICS = {
    "vllm:num_requests_running": "running",
    "vllm:num_requests_waiting": "waiting",
    "vllm:gpu_cache_usage_perc": "kv_cache",
    "vllm:kv_cache_usage_perc": "kv_cache",
    "vllm:prompt_tokens_total": "prompt_tokens_total",
    "vllm:generation_tokens_total": "generation_tokens_total",
    "vllm:num_preemptions_total": "preemptions_total",
}
_METRIC_LINE = re.compile(r"^([a-zA-Z_:][\w:]*)(\{[^}]*\})?\s+([-+0-9.eE]+|NaN|\+Inf)")


def _percentile(values: list[float], share: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    index = min(len(values) - 1, max(0, math.ceil(share * len(values)) - 1))
    return round(values[index], 1)


def overview(store: Store, range_key: str = "24h", now: float | None = None) -> dict[str, Any]:
    now = now or time.time()
    span = RANGES.get(range_key, RANGES["24h"])
    since = now - span
    rows = store.all("SELECT ts, user_id, model_id, endpoint, status, prompt_tokens, completion_tokens, latency_ms, "
                     "ttft_ms, stream, error, guard FROM usage WHERE ts >= ? ORDER BY ts", (since,))
    users = {u["id"]: u["username"] for u in store.all("SELECT id, username FROM users")}
    traffic = [r for r in rows if r["endpoint"] != "guard"]
    total = {
        "requests": len(traffic),
        "prompt_tokens": sum(r["prompt_tokens"] for r in traffic),
        "completion_tokens": sum(r["completion_tokens"] for r in traffic),
        "errors": sum(1 for r in traffic if r["status"] >= 500 or (r["status"] >= 400 and not r["guard"]
                                                                  and r["status"] != 429)),
        "refused": sum(1 for r in traffic if r["status"] in (403, 429) or r["guard"].startswith(("block", "model",
                                                                                                  "max_input"))),
        "users": len({r["user_id"] for r in traffic if r["user_id"]}),
    }
    per_model: dict[str, dict[str, Any]] = {}
    for row in traffic:
        item = per_model.setdefault(row["model_id"], {"model": row["model_id"], "requests": 0, "prompt_tokens": 0,
                                                      "completion_tokens": 0, "errors": 0, "_lat": [], "_ttft": [],
                                                      "_gen_s": 0.0, "_gen_tokens": 0})
        item["requests"] += 1
        item["prompt_tokens"] += row["prompt_tokens"]
        item["completion_tokens"] += row["completion_tokens"]
        if row["status"] >= 500 or (row["status"] >= 400 and row["status"] not in (403, 429) and not row["guard"]):
            item["errors"] += 1
        if row["status"] == 200:
            item["_lat"].append(row["latency_ms"])
            if row["ttft_ms"] is not None:
                item["_ttft"].append(row["ttft_ms"])
                generating = (row["latency_ms"] - row["ttft_ms"]) / 1000
                if generating > 0.05 and row["completion_tokens"] > 1:
                    item["_gen_s"] += generating
                    item["_gen_tokens"] += row["completion_tokens"]
    models = []
    for item in per_model.values():
        latencies, ttfts = item.pop("_lat"), item.pop("_ttft")
        gen_s, gen_tokens = item.pop("_gen_s"), item.pop("_gen_tokens")
        item.update(p50_ms=_percentile(latencies, 0.5), p95_ms=_percentile(latencies, 0.95),
                    ttft_ms=_percentile(ttfts, 0.5), ttft_p95_ms=_percentile(ttfts, 0.95),
                    tokens_per_s=round(gen_tokens / gen_s, 1) if gen_s else None,
                    error_rate=round(item["errors"] / item["requests"], 3) if item["requests"] else 0)
        models.append(item)
    models.sort(key=lambda m: -(m["prompt_tokens"] + m["completion_tokens"]))
    step = span / BUCKETS
    series: dict[str, list[int]] = {}
    requests_series = [0] * BUCKETS
    for row in traffic:
        index = min(BUCKETS - 1, int((row["ts"] - since) / step))
        series.setdefault(row["model_id"], [0] * BUCKETS)[index] += row["prompt_tokens"] + row["completion_tokens"]
        requests_series[index] += 1
    per_user: dict[int, dict[str, Any]] = {}
    for row in traffic:
        if not row["user_id"]:
            continue
        item = per_user.setdefault(row["user_id"], {"user": users.get(row["user_id"], f"#{row['user_id']}"),
                                                    "requests": 0, "tokens": 0, "models": set(), "last": 0})
        item["requests"] += 1
        item["tokens"] += row["prompt_tokens"] + row["completion_tokens"]
        item["models"].add(row["model_id"])
        item["last"] = max(item["last"], row["ts"])
    top_users = sorted(({**u, "models": sorted(u["models"])} for u in per_user.values()), key=lambda u: -u["tokens"])
    problems = [{"ts": r["ts"], "user": users.get(r["user_id"], ""), "model": r["model_id"], "status": r["status"],
                 "error": r["error"][:200], "guard": r["guard"]}
                for r in reversed(traffic) if r["status"] >= 400 or r["error"]][:25]
    return {"range": range_key, "since": since, "until": now, "bucket_seconds": step, "total": total, "models": models,
            "series": series, "requests_series": requests_series, "users": top_users[:50], "problems": problems}


def clients(store: Store) -> list[dict[str, Any]]:
    """Devices (API keys) seen, newest first: who, from what, Dcode version."""
    return store.all(
        "SELECT k.id, k.name, k.prefix, k.client, k.created, k.last_used, k.revoked, u.username FROM api_keys k "
        "JOIN users u ON u.id = k.user_id ORDER BY k.last_used DESC LIMIT 200")


def parse_metrics(text: str) -> dict[str, float]:
    values: dict[str, float] = {}
    for line in text.splitlines():
        match = _METRIC_LINE.match(line)
        if not match or match.group(1) not in VLLM_METRICS:
            continue
        try:
            value = float(match.group(3))
        except ValueError:
            continue
        key = VLLM_METRICS[match.group(1)]
        values[key] = values.get(key, 0.0) + value  # summed over label sets (one per served model name)
    return values


def _health(model: dict[str, Any]) -> dict[str, Any]:
    item: dict[str, Any] = {"model": model["id"], "enabled": model["enabled"], "up": None, "probe_ms": None,
                            "error": "", "metrics": {}}
    if not model["enabled"]:
        return item
    started = time.perf_counter()
    listed, error = probe(model["upstream_url"], model.get("upstream_key", ""))
    item.update(up=not error, probe_ms=round((time.perf_counter() - started) * 1000, 1), error=error,
                serves=[m["id"] for m in listed][:5])
    if model.get("metrics_url") and not error:
        try:
            with urllib.request.urlopen(model["metrics_url"], timeout=3) as response:
                item["metrics"] = parse_metrics(response.read().decode(errors="replace"))
        except Exception as exc:  # noqa: BLE001
            item["metrics_error"] = str(exc)[:200]
    return item


def live(store: Store) -> list[dict[str, Any]]:
    """Every model's server asked at once: one slow or dead server doesn't hold up the page."""
    from concurrent.futures import ThreadPoolExecutor

    models = store.models()
    if not models:
        return []
    with ThreadPoolExecutor(max_workers=min(16, len(models))) as pool:
        return list(pool.map(_health, models))
