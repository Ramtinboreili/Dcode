from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# --- Paths ---------------------------------------------------------------------
# Everything lives under one per-user home. DCODE_HOME overrides it (tests,
# shared servers with custom layouts, read-only $HOME, ...).

LEGACY_HOME = Path.home() / ".code-one"


def home_dir() -> Path:
    override = os.environ.get("DCODE_HOME")
    return Path(override).expanduser() if override else Path.home() / ".dcode"


def config_file() -> Path:
    return home_dir() / "config.json"


def threads_dir() -> Path:
    return home_dir() / "threads"


def skills_dir() -> Path:
    return home_dir() / "skills"


def memory_dir() -> Path:
    return home_dir() / "memory"


def run_dir() -> Path:
    """Runtime state: hub info, live-session registry, auth token."""
    return home_dir() / "run"


# --- Defaults ------------------------------------------------------------------

DEFAULT_REASONING_EFFORT = "medium"
REASONING_EFFORTS = ["none", "low", "medium", "high"]

DEFAULT_PERMISSIONS = {
    "read_file": True,
    "write_file": True,
    "run_command": True,
    "delete_file": True,
    "create_directory": True,
    "search_in_files": True,
}
DEFAULT_EXPERIMENTAL = {
    "multi_file_edit": False,
    "auto_commit": False,
    "streaming_tools": False,
    # When True, run_command executes without a per-command confirmation prompt.
    # Leave OFF unless you fully trust the model and the environment.
    "auto_approve_commands": False,
}
DEFAULT_AGENTS = {
    "enabled": True,        # expose the `delegate` tool to the main agent
    "max_parallel": 2,      # read-only sub-agents that may run at once
    "max_steps": 16,        # tool-loop iterations per sub-agent
    "helper_model": "",     # run helpers on another model (e.g. a small fast one); "" = the main model
}
DEFAULT_MEMORY = {
    "enabled": True,        # expose memory tools + inject memory index
    "auto_recall": True,    # inject relevant memories for each new task
    "budget_tokens": 1200,  # max tokens of memory injected per turn
}
DEFAULT_ARTIFACTS = {
    "enabled": True,        # expose artifact_save / artifact_get to the main agent
}
DEFAULT_CHECKS = {
    "enabled": True,        # check each file right after Dcode edits it; errors go back to the model
    "format": False,        # also run a formatter (ruff/black, gofmt, rustfmt, the project's prettier)
    "timeout": 15,          # seconds per check
    "commands": {},         # your own checks per extension, e.g. {".ts": "npx --no-install tsc --noEmit -p ."}
    "formatters": {},       # your own formatters per extension, e.g. {".py": "black -q $FILE"}
}
DEFAULT_RULES = {
    # pattern -> "allow" | "ask" | "deny"; the last matching pattern wins. See dcode/rules.py.
    "commands": {},
    "files": {".env": "deny", ".env.*": "deny", "*.env": "deny",
              ".env.example": "allow", ".env.sample": "allow", "*.env.example": "allow"},
    "outside_workspace": "ask",   # files outside the project folder
}
DEFAULT_UI = {
    "enabled": True,        # start / join the localhost hub UI
    "port": 0,              # 0 = deterministic per-user port
    "open_browser": False,
}
DEFAULT_CONTEXT_WINDOW = 32768
DEFAULT_MAX_OUTPUT_TOKENS = 4096


@dataclass
class AppConfig:
    base_url: str
    api_key: str
    model: str
    reasoning_effort: str = DEFAULT_REASONING_EFFORT
    context_window: int = DEFAULT_CONTEXT_WINDOW
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    stream: bool = True
    permissions: dict[str, bool] = field(default_factory=lambda: dict(DEFAULT_PERMISSIONS))
    experimental: dict[str, bool] = field(default_factory=lambda: dict(DEFAULT_EXPERIMENTAL))
    agents: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_AGENTS))
    memory: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_MEMORY))
    ui: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_UI))
    artifacts: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_ARTIFACTS))
    checks: dict[str, Any] = field(default_factory=lambda: json.loads(json.dumps(DEFAULT_CHECKS)))
    rules: dict[str, Any] = field(default_factory=lambda: json.loads(json.dumps(DEFAULT_RULES)))
    enabled_skills: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "api_key": self.api_key,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "stream": self.stream,
            "permissions": dict(self.permissions),
            "experimental": dict(self.experimental),
            "agents": dict(self.agents),
            "memory": dict(self.memory),
            "ui": dict(self.ui),
            "artifacts": dict(self.artifacts),
            "checks": json.loads(json.dumps(self.checks)),
            "rules": json.loads(json.dumps(self.rules)),
            "enabled_skills": list(self.enabled_skills),
        }


def ensure_app_dirs() -> None:
    _migrate_legacy_home()
    for path in (home_dir(), threads_dir(), skills_dir(), memory_dir(), run_dir()):
        path.mkdir(parents=True, exist_ok=True)
    # The home holds an API key in plaintext; keep it private (0700).
    _chmod_quiet(home_dir(), 0o700)
    _chmod_quiet(run_dir(), 0o700)
    from .skills import seed_builtin_skills  # local import: skills imports config

    seed_builtin_skills()


def _migrate_legacy_home() -> None:
    """Copy ~/.code-one into the Dcode home, once (originals untouched).

    Keyed on the config file rather than the directory: runtime folders such as
    ~/.dcode/run can exist before any config does.
    """
    if os.environ.get("DCODE_HOME"):
        return
    target = home_dir()
    if (target / "config.json").exists() or not LEGACY_HOME.is_dir():
        return
    try:
        shutil.copytree(LEGACY_HOME, target, dirs_exist_ok=True)
    except OSError:
        pass


def _chmod_quiet(path: Path, mode: int) -> None:
    """Best-effort chmod; silently ignore platforms/filesystems that reject it."""
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    return data if isinstance(data, dict) else {}


def write_private_json(path: Path, payload: Any) -> None:
    """Atomically write JSON readable only by the owner."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
    os.replace(tmp, path)
    _chmod_quiet(path, 0o600)


def _merge_defaults(source: dict[str, Any] | None, defaults: dict[str, Any]) -> dict[str, Any]:
    """Overlay known keys from ``source`` onto ``defaults``, coercing to the default's type."""
    merged = dict(defaults)
    if isinstance(source, dict):
        for key, value in source.items():
            if key not in merged:
                continue
            default = defaults[key]
            try:
                merged[key] = bool(value) if isinstance(default, bool) else type(default)(value)
            except (TypeError, ValueError):
                pass
    return merged


def _env(name: str) -> str | None:
    """Read DCODE_<name>, falling back to the legacy CODE_ONE_<name>."""
    return os.environ.get(f"DCODE_{name}") or os.environ.get(f"CODE_ONE_{name}")


ENV_KEYS = {
    "base_url": "BASE_URL",
    "api_key": "API_KEY",
    "model": "MODEL",
    "reasoning_effort": "REASONING_EFFORT",
    "context_window": "CONTEXT_WINDOW",
    "max_output_tokens": "MAX_OUTPUT_TOKENS",
}


def env_locked_keys() -> list[str]:
    """Settings currently pinned by DCODE_* / CODE_ONE_* environment variables."""
    return [key for key, name in ENV_KEYS.items() if _env(name)]


def apply_env_overrides(data: dict[str, Any]) -> dict[str, Any]:
    """Environment variables win over the saved config file.

    This is the primary way to configure Dcode in an air-gapped / headless
    deployment where no interactive setup wizard is run.
    """
    for key, env_name in ENV_KEYS.items():
        value = _env(env_name)
        if value:
            data[key] = value
    if _env("NO_UI"):
        data.setdefault("ui", {})
        if isinstance(data["ui"], dict):
            data["ui"]["enabled"] = False
    return data


def _int(value: Any, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return default


def config_from_dict(data: dict[str, Any]) -> AppConfig | None:
    base_url = data.get("base_url")
    api_key = data.get("api_key")
    model = data.get("model")
    if not base_url or not api_key or not model:
        return None
    enabled_skills = data.get("enabled_skills")
    if not isinstance(enabled_skills, list):
        enabled_skills = []
    effort = str(data.get("reasoning_effort") or DEFAULT_REASONING_EFFORT)
    return AppConfig(
        base_url=str(base_url),
        api_key=str(api_key),
        model=str(model),
        reasoning_effort=effort if effort in REASONING_EFFORTS else DEFAULT_REASONING_EFFORT,
        context_window=_int(data.get("context_window"), DEFAULT_CONTEXT_WINDOW, 2048),
        max_output_tokens=_int(data.get("max_output_tokens"), DEFAULT_MAX_OUTPUT_TOKENS, 256),
        stream=bool(data.get("stream", True)),
        permissions=_merge_defaults(data.get("permissions"), DEFAULT_PERMISSIONS),
        experimental=_merge_defaults(data.get("experimental"), DEFAULT_EXPERIMENTAL),
        agents=_merge_defaults(data.get("agents"), DEFAULT_AGENTS),
        memory=_merge_defaults(data.get("memory"), DEFAULT_MEMORY),
        ui=_merge_defaults(data.get("ui"), DEFAULT_UI),
        artifacts=_merge_defaults(data.get("artifacts"), DEFAULT_ARTIFACTS),
        # Deep copies: these hold nested dicts that must never alias the module defaults.
        checks=json.loads(json.dumps(_merge_defaults(data.get("checks"), DEFAULT_CHECKS))),
        rules=json.loads(json.dumps(_merge_defaults(data.get("rules"), DEFAULT_RULES))),
        enabled_skills=[str(name) for name in enabled_skills],
    )


def load_raw_config() -> dict[str, Any]:
    ensure_app_dirs()
    if config_file().exists():
        try:
            return _load_json(config_file())
        except (json.JSONDecodeError, OSError):
            # A corrupt config must not crash startup; treat it as absent.
            return {}
    return {}


def load_config() -> AppConfig | None:
    return config_from_dict(apply_env_overrides(load_raw_config()))


def save_config(config: AppConfig) -> None:
    ensure_app_dirs()
    # The file contains an API key in plaintext; restrict to owner only.
    write_private_json(config_file(), config.as_dict())


# --- Threads (saved conversations) ----------------------------------------------

def slugify_thread_name(name: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in name.strip())
    cleaned = "-".join(part for part in cleaned.split("-") if part)
    return cleaned or datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def thread_path(name: str) -> Path:
    ensure_app_dirs()
    return threads_dir() / f"{slugify_thread_name(name)}.json"


def save_thread(
    name: str,
    messages: list[dict[str, Any]],
    created_at: str | None = None,
    extra: dict[str, Any] | None = None,
) -> Path:
    ensure_app_dirs()
    payload: dict[str, Any] = {
        "name": name,
        "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "messages": messages,
    }
    payload.update(extra or {})
    path = thread_path(name)
    write_private_json(path, payload)
    return path


def load_thread(path: Path) -> dict[str, Any]:
    return _load_json(path)


def list_threads() -> list[dict[str, Any]]:
    ensure_app_dirs()
    items: list[dict[str, Any]] = []
    for path in threads_dir().glob("*.json"):
        try:
            payload = _load_json(path)
        except Exception:
            continue
        items.append(
            {
                "path": path,
                "id": path.stem,
                "name": payload.get("name") or path.stem,
                "created_at": payload.get("created_at") or "",
                "updated_at": payload.get("updated_at") or payload.get("created_at") or "",
                "cwd": payload.get("cwd") or "",
                "model": payload.get("model") or "",
                "messages": payload.get("messages") or [],
            }
        )
    items.sort(key=lambda item: str(item["updated_at"]), reverse=True)
    return items


def list_skill_files() -> list[Path]:
    ensure_app_dirs()
    return sorted(skills_dir().glob("*.md"))
