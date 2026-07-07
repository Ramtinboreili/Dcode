from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


CONFIG_DIR = Path.home() / ".code-one"
CONFIG_FILE = CONFIG_DIR / "config.json"
THREADS_DIR = CONFIG_DIR / "threads"
SKILLS_DIR = CONFIG_DIR / "skills"

DEFAULT_REASONING_EFFORT = "medium"
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
DEFAULT_SKILLS = {
    "code_review.md": False,
}


@dataclass
class AppConfig:
    base_url: str
    api_key: str
    model: str
    reasoning_effort: str = DEFAULT_REASONING_EFFORT
    permissions: Dict[str, bool] = field(default_factory=lambda: dict(DEFAULT_PERMISSIONS))
    experimental: Dict[str, bool] = field(default_factory=lambda: dict(DEFAULT_EXPERIMENTAL))
    enabled_skills: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "base_url": self.base_url,
            "api_key": self.api_key,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "permissions": dict(self.permissions),
            "experimental": dict(self.experimental),
            "enabled_skills": list(self.enabled_skills),
        }


def ensure_app_dirs() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    THREADS_DIR.mkdir(parents=True, exist_ok=True)
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    # The config dir holds an API key in plaintext; keep it private (0700).
    _chmod_quiet(CONFIG_DIR, 0o700)
    default_skill = SKILLS_DIR / "code_review.md"
    if not default_skill.exists():
        default_skill.write_text(
            "\n".join(
                [
                    "# Code Review",
                    "",
                    "- Focus on bugs, regressions, and missing tests.",
                    "- Call out risky assumptions and edge cases.",
                    "- Prefer concise, actionable feedback.",
                ]
            )
            + "\n",
            encoding="utf-8",
        )


def _chmod_quiet(path: Path, mode: int) -> None:
    """Best-effort chmod; silently ignore platforms/filesystems that reject it."""
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    return data if isinstance(data, dict) else {}


def _merge_defaults(source: Optional[Dict[str, Any]], defaults: Dict[str, bool]) -> Dict[str, bool]:
    merged = dict(defaults)
    if isinstance(source, dict):
        for key, value in source.items():
            if key in merged:
                merged[key] = bool(value)
    return merged


def _apply_env_overrides(data: Dict[str, Any]) -> Dict[str, Any]:
    """Environment variables win over the saved config file.

    This is the primary way to configure code-one in an air-gapped /
    headless deployment where no interactive setup wizard is run.
    """
    env_map = {
        "base_url": "CODE_ONE_BASE_URL",
        "api_key": "CODE_ONE_API_KEY",
        "model": "CODE_ONE_MODEL",
        "reasoning_effort": "CODE_ONE_REASONING_EFFORT",
    }
    for key, env_name in env_map.items():
        value = os.environ.get(env_name)
        if value:
            data[key] = value
    return data


def load_config() -> Optional[AppConfig]:
    ensure_app_dirs()
    data: Dict[str, Any] = {}
    if CONFIG_FILE.exists():
        try:
            data = _load_json(CONFIG_FILE)
        except (json.JSONDecodeError, OSError):
            # A corrupt config must not crash startup; treat it as absent.
            data = {}

    data = _apply_env_overrides(data)
    base_url = data.get("base_url")
    api_key = data.get("api_key")
    model = data.get("model")
    if not base_url or not api_key or not model:
        return None

    reasoning_effort = str(data.get("reasoning_effort") or DEFAULT_REASONING_EFFORT)
    permissions = _merge_defaults(data.get("permissions"), DEFAULT_PERMISSIONS)
    experimental = _merge_defaults(data.get("experimental"), DEFAULT_EXPERIMENTAL)
    enabled_skills = data.get("enabled_skills")
    if not isinstance(enabled_skills, list):
        enabled_skills = []
    return AppConfig(
        base_url=base_url,
        api_key=api_key,
        model=model,
        reasoning_effort=reasoning_effort,
        permissions=permissions,
        experimental=experimental,
        enabled_skills=[str(name) for name in enabled_skills],
    )


def save_config(config: AppConfig) -> None:
    ensure_app_dirs()
    with CONFIG_FILE.open("w", encoding="utf-8") as handle:
        json.dump(config.as_dict(), handle, indent=2)
    # The file contains an API key in plaintext; restrict to owner only.
    _chmod_quiet(CONFIG_FILE, 0o600)


def slugify_thread_name(name: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in name.strip())
    cleaned = "-".join(part for part in cleaned.split("-") if part)
    return cleaned or datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def thread_path(name: str) -> Path:
    ensure_app_dirs()
    return THREADS_DIR / f"{slugify_thread_name(name)}.json"


def save_thread(name: str, messages: List[Dict[str, Any]], created_at: Optional[str] = None) -> Path:
    ensure_app_dirs()
    created = created_at or datetime.now(timezone.utc).isoformat()
    path = thread_path(name)
    with path.open("w", encoding="utf-8") as handle:
        json.dump({"name": name, "created_at": created, "messages": messages}, handle, indent=2)
    return path


def load_thread(path: Path) -> Dict[str, Any]:
    return _load_json(path)


def list_threads() -> List[Dict[str, Any]]:
    ensure_app_dirs()
    items: List[Dict[str, Any]] = []
    for path in THREADS_DIR.glob("*.json"):
        try:
            payload = _load_json(path)
        except Exception:
            continue
        items.append(
            {
                "path": path,
                "name": payload.get("name") or path.stem,
                "created_at": payload.get("created_at") or "",
                "messages": payload.get("messages") or [],
            }
        )
    items.sort(key=lambda item: str(item["created_at"]), reverse=True)
    return items


def list_skill_files() -> List[Path]:
    ensure_app_dirs()
    return sorted(SKILLS_DIR.glob("*.md"))
