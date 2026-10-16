"""Dcode's side of a Dcode Manager: sign in, take the API key and the model list, show usage.

``dcode login <url>`` and the setup's "Sign in to a Dcode Manager" use ``device_login``; the key then
works like any API key, with the manager's ``/v1`` as the base URL. Standard library only.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from typing import Any

from . import __version__
from .config import AppConfig

TIMEOUT = 20


class ManagerError(Exception):
    pass


def normalize_url(url: str) -> str:
    """"mgr.corp:8080/v1/" → "http://mgr.corp:8080" (the manager's root; the gateway is <root>/v1)."""
    url = (url or "").strip().rstrip("/")
    if url and "://" not in url:
        url = "http://" + url
    if url.endswith("/v1"):
        url = url[:-3]
    return url.rstrip("/")


def client_name() -> str:
    return f"Dcode {__version__}"


def _request(url: str, body: Any = None, key: str = "") -> Any:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    request.add_header("Accept", "application/json")
    request.add_header("User-Agent", client_name())
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if key:
        request.add_header("Authorization", f"Bearer {key}")
    context = None
    if url.startswith("https://"):
        import ssl

        context = ssl.create_default_context()
        try:
            import certifi  # python.org builds on macOS have no CA store

            context.load_verify_locations(certifi.where())
        except Exception:  # noqa: BLE001 - the system store is enough on Linux
            pass
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT, context=context) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read() or b"{}")
            error = payload.get("error")
            message = error.get("message") if isinstance(error, dict) else error
        except ValueError:
            message = ""
        raise ManagerError(message or f"The manager answered {exc.code}.") from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        raise ManagerError(f"Can't reach the manager at {url.split('/api/')[0]}: {reason}") from None


def info(url: str) -> dict[str, Any]:
    """The manager at ``url`` (raises ManagerError if it isn't one)."""
    payload = _request(normalize_url(url) + "/api/info")
    if not isinstance(payload, dict) or payload.get("service") != "dcode-manager":
        raise ManagerError(f"{url} is not a Dcode Manager.")
    return payload


def device_login(url: str, username: str, password: str) -> dict[str, Any]:
    """{api_key, base_url, user, models} for this device."""
    return _request(normalize_url(url) + "/api/device/login", {
        "username": username, "password": password, "device": socket.gethostname()[:60], "client": client_name()})


def usage(config: AppConfig) -> dict[str, Any]:
    if not config.manager.get("url"):
        raise ManagerError("Not signed in to a Dcode Manager (run `dcode login <url>`).")
    return _request(config.manager["url"] + "/api/me/usage", key=config.api_key)


def models(config: AppConfig) -> list[dict[str, Any]]:
    return list((_request(config.base_url.rstrip("/") + "/models", key=config.api_key) or {}).get("data") or [])


def remember_models(config: AppConfig, listed: list[dict[str, Any]]) -> None:
    config.manager["models"] = {
        m["id"]: {k: m[k] for k in ("family", "context_length", "max_output_tokens", "kind") if m.get(k)}
        for m in listed if m.get("id")}


def use_model(config: AppConfig, model: str) -> None:
    """Switch to ``model`` with the context length and output cap the manager gives it."""
    config.model = model
    known = config.manager.get("models", {}).get(model) or {}
    if known.get("context_length"):
        config.context_window = max(2048, int(known["context_length"]))
    if known.get("max_output_tokens"):
        config.max_output_tokens = max(256, min(config.max_output_tokens, int(known["max_output_tokens"])))


def is_manager(config: AppConfig) -> bool:
    url = config.manager.get("url") or ""
    return bool(url) and config.base_url.rstrip("/").startswith(url)


def apply_login(config: AppConfig, url: str, username: str, result: dict[str, Any], model: str = "") -> None:
    config.base_url = result.get("base_url") or normalize_url(url) + "/v1"
    config.api_key = result["api_key"]
    config.manager = {"url": normalize_url(url), "username": username, "models": {}}
    remember_models(config, result.get("models") or [])
    choices = [m["id"] for m in result.get("models") or [] if (m.get("kind") or "chat") == "chat"]
    use_model(config, model if model in choices else (choices[0] if choices else config.model))


def usage_lines(summary: dict[str, Any]) -> list[str]:
    """Plain lines for /usage and `dcode login`: the total, then each model with its limit and reset."""
    import datetime

    def when(ts: Any) -> str:
        return datetime.datetime.fromtimestamp(ts).strftime("%a %d %b %H:%M") if ts else "only by an admin"

    def line(label: str, item: dict[str, Any]) -> str:
        used = f"{item['used']:,} tokens"
        if item.get("limit") is None:
            return f"{label}: {used} this {item['period'] if item['period'] != 'none' else 'period'} · no limit"
        share = min(100, round(100 * item["used"] / max(1, item["limit"])))
        return (f"{label}: {used} of {item['limit']:,} ({share}%) · {item['left']:,} left · resets "
                f"{when(item.get('resets_at'))}")

    out = [line("All models", summary["total"])]
    for item in summary.get("models") or []:
        out.append(line(item["model"] + ("" if item.get("enabled", True) else " (off)"), item))
    return out
