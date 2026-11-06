"""The ``experiment`` config section: every mechanism a study may switch or tune, in one place.

The defaults are today's behaviour, so a config without this section sends the same requests as one with
every key spelled out. A run is described by the effective section and its hash (written to each trace), so
a typo must never quietly run the baseline instead: unknown keys and wrong types raise ExperimentError.
Standard library only: the hub imports config.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    # History: stubbing the big outputs of turns the model already answered
    "earlier_turn_elision": True,
    "earlier_turn_min_chars": 1500,
    # Compaction
    "compact_trigger": 0.75,
    "compact_target": 0.45,
    "keep_recent_tool_results": 3,
    "elide_min_chars": 600,
    "summarization": True,
    # Long tasks (see docs/research/experiment-config.md for why these are on by default)
    "shrink_old_args": True,         # stage 1 also stubs the big arguments of old calls
    "shrink_args_tools": ["write_file", "edit_file"],
    "shrink_args_min_chars": 240,
    "intra_task_summarize": True,    # stage 2 may summarize older steps of the running task
    "intra_task_trigger": 1.0,       # ...when still over this share of the input budget after the rest
    "overflow_recovery": True,       # the server says it's too long: compact hard and try once more
    # Big arguments of calls whose payload is stored elsewhere
    "payload_stubbing": True,
    "payload_stub_tools": ["artifact_save", "skill_save"],
    "payload_stub_min_chars": 240,
    # Tool output cap: share of the window, then a floor and a ceiling, in characters
    "output_cap_share": 0.12,
    "output_cap_min": 3000,
    "output_cap_max": 60000,
    # Prompt size
    "skills_mode": "index",
    "mcp_schemas": "names",
    "recall_placement": "system",
    # Reasoning cut-off
    "think_share": 0.75,
    "think_retries": 2,
    "loop_check_every": 2000,
    # Guards and follow-through
    "repeat_limit": 3,
    "stuck_limit": 2,
    "main_max_steps": 60,
    "max_follow_ups": 2,
    # Other mechanisms
    "text_tool_recovery": True,
    "delegation": True,
    # Sampling: sent only when set
    "temperature": None,
    "top_p": None,
    "seed": None,
}

CHOICES = {
    "skills_mode": ("index", "inline"),
    "mcp_schemas": ("names", "full"),
    "recall_placement": ("system", "user"),
}
# (smallest, largest) a number may be; None = no bound.
RANGES: dict[str, tuple[float | None, float | None]] = {
    "earlier_turn_min_chars": (0, None),
    "compact_trigger": (0.01, 1.0),
    "compact_target": (0.01, 1.0),
    "keep_recent_tool_results": (0, None),
    "elide_min_chars": (0, None),
    "payload_stub_min_chars": (0, None),
    "shrink_args_min_chars": (0, None),
    "intra_task_trigger": (0.01, 1.0),
    "output_cap_share": (0.001, 1.0),
    "output_cap_min": (1, None),
    "output_cap_max": (1, None),
    "think_share": (0.01, None),
    "think_retries": (0, None),
    "loop_check_every": (0, None),  # 0 = no checks while the reasoning streams
    "repeat_limit": (2, None),
    "stuck_limit": (1, None),
    "main_max_steps": (1, None),
    "max_follow_ups": (0, None),
    "temperature": (0.0, None),
    "top_p": (0.0, 1.0),
}
OPTIONAL_FLOATS = {"temperature", "top_p"}
OPTIONAL_INTS = {"seed"}


class ExperimentError(ValueError):
    """The experiment section is wrong. Never ignored: it would silently change what a run measures."""


def defaults() -> dict[str, Any]:
    return json.loads(json.dumps(DEFAULTS))


def _check(key: str, value: Any) -> Any:
    default = DEFAULTS[key]
    if key in OPTIONAL_FLOATS and value is None or key in OPTIONAL_INTS and value is None:
        return None
    wrong = ExperimentError(f"experiment.{key}: {value!r} is not valid (default: {default!r})")
    if key in CHOICES:
        if value not in CHOICES[key]:
            raise ExperimentError(f"experiment.{key}: {value!r} is not one of {', '.join(CHOICES[key])}")
        return value
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise wrong
        return value
    if isinstance(default, list):
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise wrong
        return list(value)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise wrong
    if key in OPTIONAL_INTS or isinstance(default, int):
        if int(value) != value:
            raise wrong
        value = int(value)
    else:
        value = float(value)
    low, high = RANGES.get(key, (None, None))
    if (low is not None and value < low) or (high is not None and value > high):
        raise wrong
    return value


def clean(section: Any) -> dict[str, Any]:
    """Defaults overlaid with ``section``; raises ExperimentError on anything it can't honour."""
    merged = defaults()
    if section is None:
        return merged
    if not isinstance(section, dict):
        raise ExperimentError("the experiment section must be a JSON object")
    unknown = sorted(set(section) - set(DEFAULTS))
    if unknown:
        raise ExperimentError(f"unknown experiment key(s): {', '.join(unknown)}. See docs/research/experiment-config.md")
    for key, value in section.items():
        merged[key] = _check(key, value)
    if merged["compact_target"] > merged["compact_trigger"]:
        raise ExperimentError("experiment.compact_target must not be above compact_trigger")
    return merged


def read_file(path: str) -> dict[str, Any]:
    """The JSON file named by DCODE_EXPERIMENT. A file that can't be read stops the run, it isn't skipped."""
    try:
        data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ExperimentError(f"DCODE_EXPERIMENT: can't read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ExperimentError(f"DCODE_EXPERIMENT: {path} must hold a JSON object")
    return data


def config_hash(section: dict[str, Any]) -> str:
    """Same effective settings, same hash, however they were written."""
    canonical = json.dumps(clean(section), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def overrides(section: dict[str, Any]) -> dict[str, Any]:
    """Only what differs from the defaults: the readable part of a run's description."""
    return {key: value for key, value in clean(section).items() if value != DEFAULTS[key]}
