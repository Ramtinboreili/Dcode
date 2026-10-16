"""Entry point for ``python -m dcode`` and the packaged binary."""

import sys


def main() -> None:
    if sys.argv[1:2] == ["hub"]:
        # The background hub skips the terminal app (and its libraries) entirely.
        from .server import hub_main

        hub_main(sys.argv[2:])
        return
    if sys.argv[1:2] == ["run"]:
        # One unattended task (scheduled jobs): no terminal app either.
        from .headless import main as run_task

        sys.exit(run_task(sys.argv[2:]))
    if sys.argv[1:2] == ["manager"]:
        # The team's model gateway and its web UI: a server, standard library only.
        from .manager.cli import main as manage

        sys.exit(manage(sys.argv[2:]))
    if sys.argv[1:2] == ["acp"]:
        # An editor drives Dcode over stdin/stdout (Agent Client Protocol): no terminal app.
        from .acp import main as serve_editor

        sys.exit(serve_editor(sys.argv[2:]))
    from .main import cli

    cli()


if __name__ == "__main__":
    main()
