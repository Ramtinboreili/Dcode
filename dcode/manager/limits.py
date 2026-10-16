"""Who may use which model, how many tokens, and when the count starts over.

A user's tokens are limited twice: per model (the user's own limit on that model, else the model's
default limit for everyone) and in total across models (the user's limit, else the default user
limit in settings). Each limit has a period: ``day``, ``week`` (from Monday), ``month`` or ``none``
(only an admin reset starts it over). Usage is counted from the start of the period or the last
reset, whichever is later; the usage table is the only counter, so a reset just moves that start.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any

from .store import Store

PERIODS = ("day", "week", "month", "none")


def zone(name: str) -> tzinfo:
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name or "UTC")
    except Exception:  # no tzdata in the image, or a typo: UTC rather than failing every request
        return timezone.utc


def period_bounds(period: str | None, now: float, tz: tzinfo) -> tuple[float, float | None]:
    """(start, next reset) of the period holding ``now``; ``none`` starts at 0 and never resets."""
    if period not in ("day", "week", "month"):
        return 0.0, None
    local = datetime.fromtimestamp(now, tz)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "day":
        end = start + timedelta(days=1)
    elif period == "week":
        start -= timedelta(days=start.weekday())
        end = start + timedelta(days=7)
    else:
        start = start.replace(day=1)
        end = (start + timedelta(days=32)).replace(day=1)
    # Wall-clock midnight in the zone (DST-safe): rebuild from the date, not by adding seconds.
    start = datetime(start.year, start.month, start.day, tzinfo=tz)
    end = datetime(end.year, end.month, end.day, tzinfo=tz)
    return start.timestamp(), end.timestamp()


@dataclass
class Limit:
    scope: str             # "model" or "total"
    model_id: str          # "" for total
    limit: int | None      # None = unlimited
    period: str
    used: int
    since: float
    resets_at: float | None

    @property
    def left(self) -> int | None:
        return None if self.limit is None else max(0, self.limit - self.used)

    @property
    def exceeded(self) -> bool:
        return self.limit is not None and self.used >= self.limit

    def public(self) -> dict[str, Any]:
        return {"scope": self.scope, "model": self.model_id, "limit": self.limit, "period": self.period,
                "used": self.used, "left": self.left, "since": self.since, "resets_at": self.resets_at}


def _used(store: Store, user_id: int, since: float, model_id: str | None = None) -> int:
    sql = "SELECT COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS n FROM usage WHERE user_id = ? AND ts >= ?"
    args: list[Any] = [user_id, since]
    if model_id is not None:
        sql += " AND model_id = ?"
        args.append(model_id)
    return int(store.one(sql, args)["n"])


def _since(period: str, user: dict[str, Any], settings: dict[str, Any], now: float) -> tuple[float, float | None]:
    start, end = period_bounds(period, now, zone(settings.get("timezone", "UTC")))
    return max(start, float(user.get("reset_at") or 0), float(settings.get("global_reset_at") or 0)), end


def allowed(model: dict[str, Any], row: dict[str, Any] | None, user: dict[str, Any]) -> bool:
    """May this user call this model at all? (Admins may use every model.)"""
    if row is not None and not row["allowed"]:
        return False
    if user.get("role") == "admin":
        return True
    return model.get("access") == "all" or row is not None


def model_limit(store: Store, user: dict[str, Any], model: dict[str, Any], row: dict[str, Any] | None,
                settings: dict[str, Any], now: float | None = None) -> Limit:
    now = now or time.time()
    has_own = row is not None and row.get("token_limit") is not None
    limit = row["token_limit"] if has_own else model.get("token_limit")
    period = (row.get("period") if has_own and row.get("period") else None) or model.get("period") or "month"
    since, resets = _since(period, user, settings, now)
    return Limit("model", model["id"], limit, period, _used(store, user["id"], since, model["id"]), since, resets)


def total_limit(store: Store, user: dict[str, Any], settings: dict[str, Any], now: float | None = None) -> Limit:
    now = now or time.time()
    own = user.get("token_limit") is not None
    limit = user["token_limit"] if own else settings.get("default_user_limit")
    period = (user.get("period") if own and user.get("period") else None) or settings.get("default_user_period") or "month"
    since, resets = _since(period, user, settings, now)
    return Limit("total", "", limit, period, _used(store, user["id"], since), since, resets)


def check(store: Store, user: dict[str, Any], model: dict[str, Any], settings: dict[str, Any]) -> str:
    """Why this user can't send a request to this model now ("" when they can)."""
    rows = {r["model_id"]: r for r in store.access_rows(user_id=user["id"])}
    row = rows.get(model["id"])
    if not allowed(model, row, user):
        return f"You don't have access to {model['id']}. Ask an admin."
    for limit in (model_limit(store, user, model, row, settings), total_limit(store, user, settings)):
        if limit.exceeded:
            what = f"on {model['id']}" if limit.scope == "model" else "across all models"
            when = (f" It resets {_when(limit.resets_at, settings)}." if limit.resets_at
                    else " An admin can reset it.")
            return f"Token limit reached {what}: {limit.used:,} of {limit.limit:,} this {_period_word(limit.period)}.{when}"
    return ""


def _period_word(period: str) -> str:
    return {"day": "day", "week": "week", "month": "month"}.get(period, "period")


def _when(ts: float | None, settings: dict[str, Any]) -> str:
    if not ts:
        return "never"
    local = datetime.fromtimestamp(ts, zone(settings.get("timezone", "UTC")))
    return local.strftime("on %Y-%m-%d at %H:%M ") + (settings.get("timezone") or "UTC")


def summary(store: Store, user: dict[str, Any], settings: dict[str, Any], models: list[dict[str, Any]]) -> dict[str, Any]:
    """Everything a user sees on their usage page: their total limit and one row per model they can use."""
    rows = {r["model_id"]: r for r in store.access_rows(user_id=user["id"])}
    per_model = []
    for model in models:
        row = rows.get(model["id"])
        if not allowed(model, row, user):
            continue
        per_model.append(model_limit(store, user, model, row, settings).public() | {
            "enabled": model["enabled"], "kind": model.get("kind", "chat")})
    return {"total": total_limit(store, user, settings).public(), "models": per_model,
            "timezone": settings.get("timezone") or "UTC"}
