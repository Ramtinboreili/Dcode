from __future__ import annotations

from typing import List, Optional
from urllib.parse import urlsplit, urlunsplit

import click
import httpx
from openai import OpenAI

from code_one.agent import CodeOneAgent
from code_one.config import AppConfig, DEFAULT_REASONING_EFFORT, ensure_app_dirs, load_config, save_config
from code_one import ui


_DOC_URL_SUFFIXES = (
    "/api-keys/docs",
    "/docs",
    "/redoc",
    "/openapi.json",
)

_DOC_URL_REWRITES = {
    "/api/v1/api-keys/docs": "/v1",
}


def _normalize_base_url(raw_url: str) -> str:
    raw = raw_url.strip()
    if not raw:
        return raw

    if raw.startswith("http://") or raw.startswith("https://"):
        parsed = urlsplit(raw)
    else:
        parsed = urlsplit(f"https://{raw}")

    if not parsed.netloc:
        raise ValueError("Base URL is invalid. Use a full URL like https://services.irangpu.com/v1")

    path = (parsed.path or "").rstrip("/")
    if path in _DOC_URL_REWRITES:
        path = _DOC_URL_REWRITES[path]
    for suffix in _DOC_URL_SUFFIXES:
        if path.endswith(suffix):
            path = path[: -len(suffix)] or "/"
            break
    path = path.rstrip("/")

    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _pick_model(models: List[str], preselected: Optional[str] = None) -> str:
    selected = ui.list_models_prompt(models, selected=preselected).strip()
    if selected:
        return selected
    if preselected in models:
        return preselected
    return models[0] if models else ""


def _prompt_model(preselected: Optional[str] = None) -> str:
    default_model = preselected or "gpt-4o"
    selected = ui.prompt_input("Model", default=default_model).strip()
    return selected or default_model


def _fetch_models(base_url: str, api_key: str) -> List[str]:
    models: List[str] = []

    try:
        client = OpenAI(base_url=base_url, api_key=api_key)
        models_response = client.models.list()
        for item in getattr(models_response, "data", []):
            model_id = getattr(item, "id", None) or str(item)
            if model_id:
                models.append(model_id)
        if models:
            return sorted(set(models))
    except Exception:
        pass

    response = httpx.get(
        f"{base_url.rstrip('/')}/models",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=20.0,
        follow_redirects=True,
    )
    response.raise_for_status()
    payload = response.json()
    for item in payload.get("data", []):
        if isinstance(item, dict):
            model_id = item.get("id")
        else:
            model_id = str(item)
        if model_id:
            models.append(model_id)
    return sorted(set(models))


def run_setup(base_url: Optional[str] = None, api_key: Optional[str] = None, model: Optional[str] = None) -> AppConfig:
    ensure_app_dirs()
    while not base_url:
        base_url = ui.prompt_input("Base URL")
        if not base_url:
            ui.console.print(f"[{ui.ORANGE}]Base URL is required[/]")

    normalized_base_url = _normalize_base_url(base_url)
    if normalized_base_url != base_url:
        ui.console.print(f"[dim white]Using base URL: {normalized_base_url}[/]")
        base_url = normalized_base_url

    while not api_key:
        api_key = ui.prompt_input("API Key", password=True)
        if not api_key:
            ui.console.print(f"[{ui.ORANGE}]API Key is required[/]")

    try:
        with ui.console.status(f"[{ui.ORANGE}]Loading models...[/]", spinner="dots", spinner_style=ui.ORANGE):
            models = _fetch_models(base_url, api_key)
    except Exception as exc:
        ui.console.print(f"[{ui.ORANGE}]Could not fetch models: {exc}[/]")
        models = []

    if not models:
        chosen_model = _prompt_model(preselected=model)
    else:
        chosen_model = _pick_model(models, preselected=model)

    config = AppConfig(
        base_url=base_url,
        api_key=api_key,
        model=chosen_model,
        reasoning_effort=DEFAULT_REASONING_EFFORT,
    )
    save_config(config)
    return config


def _run_setup(base_url: Optional[str], api_key: Optional[str], model: Optional[str]) -> AppConfig:
    return run_setup(base_url=base_url, api_key=api_key, model=model)


@click.command()
@click.option("--setup", is_flag=True, help="Re-run setup and save config")
@click.option("--base-url", "base_url", default=None, help="Override base URL")
@click.option("--api-key", "api_key", default=None, help="Override API key")
@click.option("--model", "model", default=None, help="Override model")
def cli(setup: bool, base_url: Optional[str], api_key: Optional[str], model: Optional[str]) -> None:
    ensure_app_dirs()
    if base_url:
        base_url = _normalize_base_url(base_url)
    config = load_config()

    if setup or not config:
        config = _run_setup(base_url, api_key, model)
    else:
        changed = False
        if base_url:
            config.base_url = base_url
            changed = True
        if api_key:
            config.api_key = api_key
            changed = True
        if model:
            config.model = model
            changed = True
        if changed:
            save_config(config)

    agent = CodeOneAgent(config)
    agent.run()


if __name__ == "__main__":
    cli()
