# Operations: build, release, install, run

## Development setup (macOS)

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -q
PYTHONPATH=. .venv/bin/dcode          # PYTHONPATH: see the hidden-.pth note below
```

**macOS quirk:** the repo lives under iCloud-synced `~/Documents`, and files created in `.venv/.../site-packages/__editable__*.pth` get the macOS `hidden` flag, which macOS re-applies even after `chflags nohidden`. Python 3.12.4 skips hidden `.pth` files, so `import dcode` only works from the repo root. Either set `PYTHONPATH=<repo>`, or keep the venv outside `~/Documents`.

## Builds

| Target | Command | Output |
|---|---|---|
| Local macOS binary (installs too) | `make mac PYTHON=.venv/bin/python` | `dist/dcode` → `/opt/homebrew/bin/dcode` (no sudo; `sudo` can't prompt inside Claude Code) |
| Linux binary | `make binary` (on a Linux host with the same CPU) | `dist/dcode` |
| Debian package | `make deb` | `dist/dcode_<ver>_<arch>.deb` |
| Offline build host | `PIP_ARGS="--no-index --find-links ./wheels" make binary` | — |

PyInstaller doesn't cross-compile. Both the web assets (`dcode/web`) and the skills (`dcode/skills`) are bundled with `--add-data`.

## Release checklist

1. Bump `__version__` in `dcode/__init__.py`.
2. Update the version in `dcode/web/guide.html`: badge and footer in **both** languages (4 places), plus the `.deb` filename in the install examples.
3. Update the `.deb` filename in `README.md`.
4. Add a `CHANGELOG.md` entry.
5. Run `pytest`, `ruff`, then `make mac` and `python docs/dev/terminal_e2e.py --binary /opt/homebrew/bin/dcode`.
6. Commit `Release X.Y.Z` and tag `git tag -a vX.Y.Z -m "Dcode X.Y.Z"`.
7. Push only when the user asks: `git push origin dev`, and push the tags separately when asked.

## Install on an air-gapped server

```bash
sudo ./install-airgap.sh dcode_0.4.0_amd64.deb   # Debian/Ubuntu
sudo ./install-airgap.sh ./dcode                 # any Linux
export DCODE_BASE_URL=http://127.0.0.1:11434/v1  DCODE_API_KEY=local
export DCODE_MODEL=qwen2.5-coder:7b              DCODE_CONTEXT_WINDOW=32768
dcode
```

## Runtime

- `dcode` starts a session and a per-session API, and spawns or joins the per-user **hub** on `127.0.0.1:7300 + uid % 500` (falling back to a free port). The welcome screen prints the link; `/ui` opens it.
- The hub exits about 6 seconds after the last session ends. If a hub from another version, or one with missing web files, is running, the next `dcode` replaces it automatically.
- Logs: `~/.dcode/run/hub.log`; background job logs in `~/.dcode/run/jobs/`.
- `dcode --no-ui` or `DCODE_NO_UI=1` runs the terminal only.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Web UI shows `{"error": "not found"}` | An old hub lost its unpacked files (fixed after 0.3.1). Close all `dcode`s; the next start replaces it. Manual fix: `kill $(pgrep -f "dcode hub")`, then start `dcode` again. |
| Model list empty in the web UI | Wrong URL or key, or TLS. The hub uses certifi's CA bundle, not the system store. You can always type a model name. |
| Links not clickable | macOS Terminal has no OSC 8 support. Use `/ui` or `/guide`, which open the browser, or ⌘-double-click the link. |
| `import dcode` fails outside the repo | The hidden `.pth` quirk above. Use `PYTHONPATH` or the built binary. |
| Settings changed in the UI have no effect | An env var pins that field (the UI shows it as read-only). Change the env var. |
| `sudo: a terminal is required` from Claude Code | Use `make mac`, which needs no sudo, or run the command in a real terminal. |
