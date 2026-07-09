"""PyInstaller onedir sidecar entry point for the Tauri desktop app.

Fixed contract with the Rust side (apps/desktop/src-tauri) — do not change
without updating that side too:
  * onedir executable named ``market-api`` (``market-api.exe`` on Windows),
    staged at apps/desktop/src-tauri/sidecar/market-api/.
  * listens on 127.0.0.1, port = env ``MARKET_SIDECAR_PORT`` (default 8765).
  * per-user app-data dir:
      macOS   ~/Library/Application Support/Market Terminal
      Windows %APPDATA%/Market Terminal
      Linux   ~/.local/share/market-terminal

``multiprocessing.freeze_support()`` must run before anything else: when this
onedir executable is re-invoked as a multiprocessing child (spawn, not fork —
the only mode Windows has, and the one PyInstaller-frozen builds use even on
posix if any dependency ever spawns), freeze_support() detects the special
bootstrap invocation, runs the child worker, and exits — before any of the
rest of this script (env setup, uvicorn, the FastAPI app import) runs again
in that child process. Nothing in this codebase currently starts a
multiprocessing worker, but apscheduler/uvicorn's dependency graph is wide
enough that this is cheap insurance and is the standard idiom for a frozen
build regardless.
"""

from __future__ import annotations

import multiprocessing

multiprocessing.freeze_support()

import logging
import os
import sys
import threading
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("market.sidecar")


def _appdata_dir() -> Path:
    """Per-user app-data directory. Inlined (no new dep) rather than reusing
    a library like ``platformdirs`` since this is the one place in the whole
    backend that needs to know about OS-specific app-data conventions, and it
    must match whatever the Rust/Tauri side resolves to independently."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Market Terminal"
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "Market Terminal"
    # Linux and anything else POSIX-ish.
    return Path.home() / ".local" / "share" / "market-terminal"


def _configure_env() -> tuple[Path, Path]:
    """Set MARKET_DATA_DIR / MARKET_SETTINGS_PATH to per-user app-data paths
    if not already set by the caller — BEFORE any ``app.*`` module is
    imported below, since app.config.Settings reads MARKET_DATA_DIR at
    construction time (pydantic-settings env var) and
    app.settings_store.SettingsStore resolves MARKET_SETTINGS_PATH at
    construction time too. Both directories are created here so a completely
    fresh install never hits a missing-parent-dir error on first write."""
    appdata = _appdata_dir()

    if not os.environ.get("MARKET_DATA_DIR"):
        os.environ["MARKET_DATA_DIR"] = str(appdata / "data")
    if not os.environ.get("MARKET_SETTINGS_PATH"):
        os.environ["MARKET_SETTINGS_PATH"] = str(appdata / "settings.json")

    data_dir = Path(os.environ["MARKET_DATA_DIR"])
    settings_path = Path(os.environ["MARKET_SETTINGS_PATH"])
    data_dir.mkdir(parents=True, exist_ok=True)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    return data_dir, settings_path


def _watchdog(parent_pid: int, poll_seconds: float = 2.0) -> None:
    """Daemon thread: exit this process if our parent (the Tauri app) dies,
    so a killed/crashed desktop app never leaves an orphaned sidecar bound to
    the port. Best-effort — see the platform note below.

    macOS/Linux: when a parent process dies, this process is reparented (to
    pid 1, or to whatever subreaper the OS/launchd uses) — a DIFFERENT pid
    than the one recorded at startup, so polling os.getppid() for a change
    reliably detects parent death.

    Windows: os.getppid() keeps returning the ORIGINAL parent's pid even
    after that process has exited (Windows does not reparent orphans the way
    POSIX does), so this check cannot detect parent death there. psutil isn't
    bundled (no new deps for this), so there is no reliable getppid-only
    signal on Windows — the watchdog is skipped entirely rather than risk a
    false-positive kill of a healthy sidecar. Caller is expected to kill the
    sidecar process directly on Windows (e.g. via its child-process handle).
    """
    if sys.platform.startswith("win"):
        log.info("parent watchdog: skipped on Windows (getppid() does not reflect parent death)")
        return
    while True:
        time.sleep(poll_seconds)
        current = os.getppid()
        if current != parent_pid:
            log.warning(
                "parent process gone (ppid changed %s -> %s) — shutting down", parent_pid, current
            )
            os._exit(1)


def main() -> None:
    data_dir, settings_path = _configure_env()
    port = int(os.environ.get("MARKET_SIDECAR_PORT", "8765"))

    log.info("market-api sidecar starting — data_dir=%s settings_path=%s port=%s",
              data_dir, settings_path, port)

    parent_pid = os.getppid()
    threading.Thread(
        target=_watchdog, args=(parent_pid,), daemon=True, name="parent-watchdog"
    ).start()

    # Imported only now — after env vars are set — so app.config.Settings and
    # app.settings_store.SettingsStore both resolve the per-user app-data
    # paths above instead of the apps/api/data/ dev default.
    import uvicorn

    from app.main import app as app_obj

    # Single process — the APScheduler jobs in app.main's lifespan are not
    # fork-safe, so this must never run with more than one uvicorn worker.
    uvicorn.run(app_obj, host="127.0.0.1", port=port, log_level="info", workers=1)


main()
