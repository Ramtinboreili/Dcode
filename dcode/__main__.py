"""Entry point for ``python -m dcode`` and the packaged binary."""

import os
import sys


def restore_host_libraries(environ=None, frozen=None) -> None:
    """Give child processes the host's own library path, not the packaged binary's.

    A PyInstaller one-file binary points LD_LIBRARY_PATH at its unpack folder (and keeps the
    original in LD_LIBRARY_PATH_ORIG). Inherited, it makes every command load our bundled
    libtinfo/libreadline: bash and awk print "no version information available" into each
    output (tokens the model reads) and some tools break. The loader read the variable when
    this process started, so changing it now only affects children.
    """
    if not (getattr(sys, "frozen", False) if frozen is None else frozen):
        return  # from source, LD_LIBRARY_PATH is the user's own
    environ = os.environ if environ is None else environ
    original = environ.pop("LD_LIBRARY_PATH_ORIG", None)
    if original:
        environ["LD_LIBRARY_PATH"] = original
    else:
        environ.pop("LD_LIBRARY_PATH", None)


def main() -> None:
    restore_host_libraries()
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
