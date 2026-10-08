"""Entry point for ``python -m dcode`` and the packaged binary."""

import sys


def main() -> None:
    if sys.argv[1:2] == ["hub"]:
        # The background hub skips the terminal app (and its libraries) entirely.
        from .server import hub_main

        hub_main(sys.argv[2:])
        return
    from .main import cli

    cli()


if __name__ == "__main__":
    main()
