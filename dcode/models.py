"""Several models, each for what it does best.

* **profiles**: named models, each on its own server if needed (one vLLM deployment
  serves one model, so a second model usually means a second URL).
* **roles**: ``reasoning`` (a stronger model for tasks that need careful thought),
  ``utility`` (a small fast one for titles, commit messages and the router's check).
  Helpers keep their own setting: ``agents.helper_model`` and a helper file's ``model:``,
  which may name a profile too.
* **router**: before each task, decide whether it goes to the reasoning model.

A role names a profile, or a bare model name on the main server; empty means the main model.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable
from typing import Any

from . import families
from .config import REASONING_EFFORTS, AppConfig, clean_family

ROUTES = ("auto", "off", "always")  # auto: the router decides · off: always main · always: always reasoning

PROFILE_KEYS = ("base_url", "api_key", "model", "context_window", "max_output_tokens", "reasoning_effort", "family")

ROUTER_PROMPT = (
    "You route tasks for a coding agent. Reply with one word. HARD: the task needs careful multi-step "
    "reasoning (debugging an unclear failure, design or architecture, algorithms, concurrency, security, "
    "a large refactor, finding a root cause). EASY: anything else (questions, small edits, running or "
    "reading things, chat)."
)

# Words that usually mean "think this through". Two of them, or one in a long request, route a task.
_HARD_RE = re.compile(
    r"\b(why|debug\w*|root[- ]cause|race condition|deadlock|leak\w*|design\w*|architect\w*|refactor\w*|"
    r"optimi[sz]\w*|performance|algorithm\w*|complex\w*|prove|proof|trade-?offs?|secur\w*|vulnerab\w*|"
    r"migrat\w*|concurren\w*|investigat\w*|diagnos\w*|figure out|analy[sz]\w*|bottleneck\w*|intermittent\w*|"
    r"flaky|crash\w*|corrupt\w*)\b",
    re.IGNORECASE,
)
_FORCE_RE = re.compile(r"\b(think (hard|carefully|deeply|it through)|ultrathink|reason carefully)\b", re.IGNORECASE)
# A short reply that answers Dcode or points back at the work ("it", "that") rather than naming a new task.
_FOLLOW_RE = re.compile(r"^(yes|yeah|yep|ok(ay)?|sure|no|nope|go|continue|proceed|again|retry|try|do|please|and|"
                        r"also|now|then|still|same)\b|\b(it|that|this|them|those)\b", re.IGNORECASE)
_TRACE_RE = re.compile(r"Traceback \(most recent call last\)|^\s+at \S+\(.*\)$|panic:|Segmentation fault|"
                       r"^\w*(Error|Exception)\b.*:", re.MULTILINE)


def clean_models(value: Any) -> dict[str, Any]:
    """The ``models`` config section, validated: unknown keys dropped, numbers coerced."""
    data = value if isinstance(value, dict) else {}
    profiles: dict[str, dict[str, Any]] = {}
    raw_profiles = data.get("profiles")
    for name, spec in (raw_profiles.items() if isinstance(raw_profiles, dict) else []):
        name = str(name).strip()
        if not name or name == "main" or not isinstance(spec, dict) or not str(spec.get("model") or "").strip():
            continue  # "main" is reserved: it always means the top-level model
        profile: dict[str, Any] = {"model": str(spec["model"]).strip()}
        for key in ("base_url", "api_key"):
            if str(spec.get(key) or "").strip():
                profile[key] = str(spec[key]).strip()
        for key, minimum in (("context_window", 2048), ("max_output_tokens", 256)):
            try:
                if spec.get(key):
                    profile[key] = max(minimum, int(spec[key]))
            except (TypeError, ValueError):
                pass
        if spec.get("reasoning_effort") in REASONING_EFFORTS:
            profile["reasoning_effort"] = spec["reasoning_effort"]
        if spec.get("family") and clean_family(spec["family"]) != "auto":
            profile["family"] = clean_family(spec["family"])
        profiles[name] = profile
    route = str(data.get("router") or "auto")
    return {
        "profiles": profiles,
        "reasoning": str(data.get("reasoning") or "").strip(),
        "utility": str(data.get("utility") or "").strip(),
        "router": route if route in ROUTES else "auto",
    }


def heuristic_route(text: str) -> tuple[bool, str]:
    """(needs the reasoning model, why). Cheap and offline; used when there is no utility model."""
    if _FORCE_RE.search(text):
        return True, "you asked it to think hard"
    if _TRACE_RE.search(text):
        return True, "an error to debug"
    hits = {m.group(0).lower() for m in _HARD_RE.finditer(text)}
    if len(hits) >= 2 or (hits and len(text) > 280):
        return True, "looks like it needs careful thought (" + ", ".join(sorted(hits)[:3]) + ")"
    if len(text) > 900:
        return True, "a long, detailed request"
    return False, ""


def is_follow_up(text: str) -> bool:
    """A short reply ("yes", "go on", "fix it") continues the last task: keep its model."""
    text = text.strip()
    return len(text.split()) <= 6 and bool(_FOLLOW_RE.search(text)) and not _HARD_RE.search(text)


class ModelPool:
    """Resolves profile names to (config, client) pairs; clients are reused so they keep what they learned."""

    def __init__(self, config: AppConfig, main_llm: Any, factory: Callable[[AppConfig], Any] | None = None) -> None:
        self.config = config
        self.main_llm = main_llm
        self.factory = factory
        self._clients: dict[tuple[str, str, str], Any] = {}

    @property
    def settings(self) -> dict[str, Any]:
        return getattr(self.config, "models", None) or clean_models(None)

    def profile_config(self, name: str) -> AppConfig | None:
        """The config a model runs with: a profile's own server and window, else the main ones."""
        name = (name or "").strip()
        if not name or name == "main" or name == self.config.model:
            return None
        spec = self.settings["profiles"].get(name) or {"model": name}  # a bare name: same server
        return dataclasses.replace(
            self.config,
            base_url=spec.get("base_url") or self.config.base_url,
            api_key=spec.get("api_key") or self.config.api_key,
            model=spec["model"],
            context_window=spec.get("context_window") or self.config.context_window,
            max_output_tokens=spec.get("max_output_tokens") or self.config.max_output_tokens,
            reasoning_effort=spec.get("reasoning_effort") or self.config.reasoning_effort,
            family=spec.get("family") or "auto",  # another model: the main model's family doesn't carry over
        )

    def resolve(self, name: str) -> tuple[AppConfig, Any] | None:
        """(config, client) for a profile or model name; None means the main model."""
        cfg = self.profile_config(name)
        if cfg is None:
            return None
        key = (cfg.base_url, cfg.api_key, cfg.model)
        client = self._clients.get(key)
        if client is None:
            same_server = (cfg.base_url, cfg.api_key) == (self.config.base_url, self.config.api_key)
            if self.factory is not None:
                client = self.factory(cfg)
            elif same_server and hasattr(self.main_llm, "with_model"):
                client = self.main_llm.with_model(cfg.model)  # shares the connection and learned quirks
            elif same_server:
                return cfg, self.main_llm  # a stand-in client that can't switch models: use it as it is
            else:
                from .llm import LLMClient  # local import: keeps this module light for the hub

                client = LLMClient(cfg)
            self._clients[key] = client
        client.config = cfg  # window, effort and reply length follow the profile
        return cfg, client

    def role(self, role: str) -> tuple[AppConfig, Any] | None:
        return self.resolve(str(self.settings.get(role) or ""))

    def utility_llm(self) -> Any:
        """The client for small side jobs (titles, commit messages): the utility model, else the main one."""
        found = self.role("utility")
        return found[1] if found else self.main_llm

    def describe(self) -> list[tuple[str, str]]:
        """(role, what runs it) for /models and the settings screen."""
        def label(name: str) -> str:
            cfg = self.profile_config(name)
            if cfg is None:
                family = families.detect(self.config.model, self.config.base_url, self.config.family)
                return f"{self.config.model} (main) · {family.label}"
            where = "" if cfg.base_url == self.config.base_url else f" @ {cfg.base_url}"
            return f"{cfg.model}{where} · {cfg.context_window:,} tokens · {families.detect(cfg.model, cfg.base_url, cfg.family).label}"

        helper = str(self.config.agents.get("helper_model") or "")
        return [("main", label("")), ("reasoning", label(self.settings["reasoning"]) if self.settings["reasoning"]
                                      else "not set (uses main)"),
                ("helpers", label(helper)), ("utility", label(self.settings["utility"]))]


def explain(pool: ModelPool, text: str, route: str, previous: bool) -> dict[str, Any]:
    """Should this task run on the reasoning model, and what settled it: hard, reason, source, utility_answer."""
    def verdict(hard: bool, reason: str, source: str, answer: str | None = None) -> dict[str, Any]:
        return {"hard": hard, "reason": reason, "source": source, "utility_answer": answer}

    if pool.role("reasoning") is None or route == "off":
        return verdict(False, "", "off")
    if route == "always":
        return verdict(True, "routing is set to always", "always")
    if previous and is_follow_up(text):
        return verdict(True, "continuing the last task", "follow_up")
    utility = pool.role("utility")
    raw: str | None = None
    if utility is not None:
        try:
            raw = utility[1].complete_text(
                [{"role": "system", "content": ROUTER_PROMPT}, {"role": "user", "content": text[:1500]}],
                max_tokens=300,  # room for a reasoning model's thinking before its one word
                purpose="router",
            )
            answer = raw.strip().upper()
        except Exception:
            answer = ""
        if answer.startswith("HARD") or answer.endswith("HARD"):
            return verdict(True, f"{utility[0].model} judged it hard", "utility", raw)
        if answer.startswith("EASY") or answer.endswith("EASY"):
            return verdict(False, "", "utility", raw)
    hard, why = heuristic_route(text)
    return verdict(hard, why, "heuristic", raw)


def decide(pool: ModelPool, text: str, route: str, previous: bool) -> tuple[bool, str]:
    """Should this task run on the reasoning model? Returns (yes, why)."""
    result = explain(pool, text, route, previous)
    return result["hard"], result["reason"]
