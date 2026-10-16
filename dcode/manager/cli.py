"""``dcode manager …``: run the manager and look after its admins.

    dcode manager serve [--host 0.0.0.0] [--port 8080] [--data DIR] [--public-url URL] [--cert F --key F]
    dcode manager create-admin USERNAME        (asks for the password, or reads DCODE_MANAGER_PASSWORD)
    dcode manager set-password USERNAME

The first admin can also come from the environment on first start (a Kubernetes Secret):
DCODE_MANAGER_ADMIN (default "admin") and DCODE_MANAGER_ADMIN_PASSWORD.
"""

from __future__ import annotations

import getpass
import os
import sys
from pathlib import Path

from . import auth
from .store import Store

USAGE = __doc__.split("\n\n", 1)[1].rsplit("\n\n", 1)[0]


def data_dir(value: str = "") -> Path:
    return Path(value or os.environ.get("DCODE_MANAGER_DATA") or Path.home() / ".dcode-manager").expanduser()


def _say(text: str) -> None:
    print(text, flush=True)


def _password(username: str) -> str:
    value = os.environ.get("DCODE_MANAGER_PASSWORD")
    if value:
        return value
    first = getpass.getpass(f"Password for {username}: ")
    if first != getpass.getpass("Again: "):
        raise SystemExit("The passwords don't match.")
    return first


def _options(argv: list[str]) -> tuple[dict[str, str], list[str]]:
    options, words, index = {}, [], 0
    while index < len(argv):
        arg = argv[index]
        if arg.startswith("--") and index + 1 < len(argv) and not argv[index + 1].startswith("--"):
            options[arg[2:]] = argv[index + 1]
            index += 2
            continue
        words.append(arg)
        index += 1
    return options, words


def bootstrap_admin(store: Store) -> str:
    """Create the first admin from the environment, if there is no user yet. Returns its name or ""."""
    password = os.environ.get("DCODE_MANAGER_ADMIN_PASSWORD", "")
    if not password or store.one("SELECT id FROM users LIMIT 1"):
        return ""
    username = os.environ.get("DCODE_MANAGER_ADMIN", "admin")
    auth.create_user(store, username, password, role="admin", name="Administrator")
    return username


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        _say(USAGE)
        return 0
    command, (options, words) = argv[0], _options(argv[1:])
    store = Store(data_dir(options.get("data", "")) / "manager.db")
    if command == "serve":
        from .server import Manager, serve

        created = bootstrap_admin(store)
        if created:
            _say(f"Created the first admin: {created}")
        if not store.one("SELECT id FROM users WHERE role = 'admin' LIMIT 1"):
            _say("No admin yet: run `dcode manager create-admin <name>` (or set DCODE_MANAGER_ADMIN_PASSWORD).")
        host, port = options.get("host", "0.0.0.0"), int(options.get("port", os.environ.get("PORT", "8080")))
        server = serve(Manager(store, options.get("public-url", os.environ.get("DCODE_MANAGER_URL", ""))), host, port,
                       options.get("cert", ""), options.get("key", ""))
        scheme = "https" if options.get("cert") else "http"
        _say(f"Dcode Manager on {scheme}://{host}:{server.server_address[1]}  (data: {store.path})")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        return 0
    if command in ("create-admin", "set-password"):
        if not words:
            _say(f"Usage: dcode manager {command} USERNAME")
            return 2
        username = words[0]
        try:
            if command == "create-admin":
                auth.create_user(store, username, _password(username), role="admin")
                _say(f"Admin {username} created.")
            else:
                user = store.user_by_name(username)
                if user is None:
                    _say(f"No user called {username}.")
                    return 1
                auth.set_password(store, user["id"], _password(username))
                _say(f"Password changed for {username}; their web sign-ins were ended.")
        except ValueError as exc:
            _say(str(exc))
            return 1
        return 0
    _say(f"Unknown command {command!r}.\n\n{USAGE}")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
