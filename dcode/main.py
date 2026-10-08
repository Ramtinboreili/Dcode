from __future__ import annotations

import signal
import sys
import webbrowser
from urllib.parse import urlsplit, urlunsplit

import click

from . import __version__, ui
from .config import (
    DEFAULT_CONTEXT_WINDOW,
    DEFAULT_REASONING_EFFORT,
    AppConfig,
    ensure_app_dirs,
    load_config,
    save_config,
)
from .theme import PRIMARY, WARNING

_DOC_URL_SUFFIXES = ("/api-keys/docs", "/docs", "/redoc", "/openapi.json")
_DOC_URL_REWRITES = {"/api/v1/api-keys/docs": "/v1"}


def _normalize_base_url(raw_url: str) -> str:
    raw = raw_url.strip()
    if not raw:
        return raw
    parsed = urlsplit(raw if raw.startswith(("http://", "https://")) else f"https://{raw}")
    if not parsed.netloc:
        raise ValueError("Base URL is invalid. Use a full URL like http://localhost:11434/v1")
    path = (parsed.path or "").rstrip("/")
    if path in _DOC_URL_REWRITES:
        path = _DOC_URL_REWRITES[path]
    for suffix in _DOC_URL_SUFFIXES:
        if path.endswith(suffix):
            path = path[: -len(suffix)] or "/"
            break
    return urlunsplit((parsed.scheme, parsed.netloc, path.rstrip("/"), "", ""))


def _fetch_models(base_url: str, api_key: str) -> list[str]:
    import httpx

    models: list[str] = []
    try:
        from openai import OpenAI

        response = OpenAI(base_url=base_url, api_key=api_key, timeout=20.0).models.list()
        models = [getattr(item, "id", None) or str(item) for item in getattr(response, "data", [])]
        if models:
            return sorted(set(models))
    except Exception:
        pass
    response = httpx.get(
        f"{base_url.rstrip('/')}/models", headers={"Authorization": f"Bearer {api_key}"},
        timeout=20.0, follow_redirects=True,
    )
    response.raise_for_status()
    for item in response.json().get("data", []):
        model_id = item.get("id") if isinstance(item, dict) else str(item)
        if model_id:
            models.append(model_id)
    return sorted(set(models))


def run_setup(base_url: str | None = None, api_key: str | None = None, model: str | None = None) -> AppConfig:
    ensure_app_dirs()
    ui.console.print(f"[bold {PRIMARY}]Dcode setup[/] — connect to any OpenAI-compatible endpoint "
                     "(Ollama, vLLM, llama.cpp, LM Studio, TGI…)")
    while not base_url:
        base_url = ui.prompt_input("Base URL (e.g. http://localhost:11434/v1)")
        if not base_url:
            ui.notice("Base URL is required", "warning")
    normalized = _normalize_base_url(base_url)
    if normalized != base_url:
        ui.notice(f"Using base URL: {normalized}")
        base_url = normalized

    while not api_key:
        api_key = ui.prompt_input("API key (any value for local servers)", password=True)
        if not api_key:
            ui.notice("API key is required", "warning")

    try:
        with ui.console.status(f"[{PRIMARY}]Loading models…[/]", spinner="dots", spinner_style=PRIMARY):
            models = _fetch_models(base_url, api_key)
    except Exception as exc:
        ui.notice(f"Could not fetch models: {exc}", "warning")
        models = []
    if models:
        chosen = ui.select_model(models, selected=model) or (models[0] if models else "")
    else:
        chosen = ui.prompt_input("Model", default=model or "qwen2.5-coder:7b") or (model or "qwen2.5-coder:7b")

    window = ui.prompt_input("Model context window in tokens", default=str(DEFAULT_CONTEXT_WINDOW))
    config = AppConfig(
        base_url=base_url,
        api_key=api_key,
        model=chosen,
        reasoning_effort=DEFAULT_REASONING_EFFORT,
        context_window=max(2048, int(window)) if window.isdigit() else DEFAULT_CONTEXT_WINDOW,
    )
    save_config(config)
    return config


def _install_signal_handlers() -> None:
    def _graceful(signum, frame):
        raise SystemExit(128 + signum)

    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, _graceful)
            except (ValueError, OSError):
                pass


def _resume_chat(session, continue_last: bool, wanted: str | None) -> bool:
    """--continue / --resume: load a saved chat before the terminal starts. True if one was loaded."""
    if not continue_last and not wanted:
        return False
    from .config import list_threads, slugify_thread_name

    threads = list_threads()  # newest first
    if wanted:
        slug, low = slugify_thread_name(wanted), wanted.lower()
        match = next((t for t in threads if t["id"] == slug), None) or next(
            (t for t in threads if low in str(t["name"]).lower()), None)
    else:
        match = next((t for t in threads if t["cwd"] == session.cwd), None)
    if match is None:
        what = f"No saved chat matches “{wanted}”" if wanted else "No saved chat for this folder yet"
        ui.console.print(f"[{WARNING}]{what} — starting a new one.[/]")
        return False
    return session.resume(match["id"])


def _start_ui(session, config: AppConfig):
    """Start this session's API, register it, and make sure the hub is up."""
    from . import server

    api = server.SessionServer(session, server.get_token()).start()
    session.port = api.port
    server.register_session({
        "id": session.id, "pid": session.pid, "port": api.port, "cwd": session.cwd,
        "model": config.model, "started_at": session.started_at, "thread_name": session.thread_name,
    })
    hub_url = server.ensure_hub(int(config.ui.get("port") or 0))
    url = server.session_url(hub_url, session.id) if hub_url else None
    if url and config.ui.get("open_browser"):
        try:
            webbrowser.open(url)
        except Exception:
            pass
    return api, url


@click.group(invoke_without_command=True, context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--setup", is_flag=True, help="Re-run setup and save config")
@click.option("--base-url", "base_url", default=None, help="Override base URL")
@click.option("--api-key", "api_key", default=None, help="Override API key")
@click.option("--model", "model", default=None, help="Override model")
@click.option("--context-window", type=int, default=None, help="Model context window (tokens)")
@click.option("--no-ui", is_flag=True, help="Don't start the localhost web UI")
@click.option("-c", "--continue", "continue_last", is_flag=True, help="Continue the last chat of this folder")
@click.option("-r", "--resume", "resume", default=None, metavar="CHAT", help="Continue a saved chat by name")
@click.version_option(__version__, prog_name="dcode")
@click.pass_context
def cli(ctx: click.Context, setup: bool, base_url: str | None, api_key: str | None, model: str | None,
        context_window: int | None, no_ui: bool, continue_last: bool, resume: str | None) -> None:
    """Dcode — a coding agent for local and air-gapped LLMs."""
    if ctx.invoked_subcommand is not None:
        return
    ensure_app_dirs()
    if base_url:
        base_url = _normalize_base_url(base_url)
    config = load_config()
    if setup or not config:
        config = run_setup(base_url, api_key, model)
    else:
        changed = False
        for key, value in (("base_url", base_url), ("api_key", api_key), ("model", model),
                           ("context_window", context_window)):
            if value:
                setattr(config, key, value)
                changed = True
        if changed:
            save_config(config)

    from .cli import TerminalApp
    from .session import Session

    _install_signal_handlers()
    session = Session(config)
    resumed = _resume_chat(session, continue_last, resume)
    api = None
    url = None
    if config.ui.get("enabled", True) and not no_ui:
        try:
            api, url = _start_ui(session, config)
            if url is None:
                ui.console.print(f"[{WARNING}]Web UI hub did not start (see ~/.dcode/run/hub.log).[/]")
        except Exception as exc:
            ui.console.print(f"[{WARNING}]Web UI unavailable: {exc}[/]")
    session.ui_url = url
    try:
        TerminalApp(session, url).run(recap=resumed)
    finally:
        session.close()
        if api is not None:
            from . import server

            server.unregister_session(session.id)
            api.stop()
        ui.console.print(f"[{PRIMARY}]Saved thread[/] {session.thread_name}. Bye.")


@cli.command("hub", hidden=True)
@click.option("--port", type=int, default=0)
def hub_command(port: int) -> None:
    """Run the per-user web UI hub (started automatically)."""
    from .server import run_hub

    run_hub(port)


@cli.command("ui")
def ui_command() -> None:
    """Print the web UI address for running sessions."""
    from .server import _read_json, get_token, hub_file, hub_healthy, live_sessions

    info = _read_json(hub_file())
    if not hub_healthy(info):
        click.echo("No Dcode UI is running (it starts with the first dcode session).")
        sys.exit(1)
    click.echo(f"{info['url']}/?token={get_token()}")
    for entry in live_sessions():
        click.echo(f"  session {entry['id']}  {entry.get('cwd', '')}")


if __name__ == "__main__":
    cli()
