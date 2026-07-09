# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build spec for the market-api desktop sidecar.

Fixed contract with the Tauri/Rust side (apps/desktop/src-tauri) — do not
rename the executable or change the onedir layout without updating that side:
  * onedir, name "market-api" (`market-api.exe` on Windows).
  * console=True — it's a background service process (not a GUI/windowed
    app); stdout/stderr carry the uvicorn/app logs.

Build:  cd apps/api && uv sync --group bundle && \
          uv run --group bundle pyinstaller --noconfirm sidecar.spec
Output: apps/api/dist/market-api/  (onedir). apps/desktop/scripts/
        build-sidecar.mjs runs the above and then copies that whole
        directory into apps/desktop/src-tauri/sidecar/market-api/.

FinBERT / transformer model WEIGHTS are never bundled here — they aren't
part of the `torch`/`transformers` PACKAGES in the first place. app/sentiment
loads them lazily at runtime from the user's normal Hugging Face cache
(~/.cache/huggingface); only the torch/transformers CODE + native libraries
are frozen in below.
"""

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

# --- hidden imports pyinstaller-hooks-contrib doesn't (or can't) infer ------
hiddenimports: list[str] = []

# ccxt.pro exchanges are looked up via getattr(ccxtpro, exchange_id, None) in
# app/ingest/crypto.py (config-driven exchange ids, e.g. "coinbase"/"kraken").
# ccxt's own __init__ files import every exchange with plain `import`
# statements (verified: no importlib/dynamic loading in ccxt/pro/__init__.py),
# so PyInstaller's static analysis already follows the whole tree once it
# sees `import ccxt.pro` in crypto.py — this collect_submodules is just cheap
# extra insurance since pyinstaller-hooks-contrib ships no dedicated ccxt hook.
hiddenimports += collect_submodules("ccxt")

# uvicorn[standard]'s "auto" loop/HTTP/WS implementations are resolved by a
# STRING key -> "module:Class" lookup at runtime (uvicorn.config.LOOP_SETUPS /
# HTTP_PROTOCOLS / WS_PROTOCOLS, resolved via importlib.import_module) —
# invisible to PyInstaller's static import graph. hook-uvicorn.py
# (collect_submodules('uvicorn')) already pulls these in transitively since
# the protocol-implementation modules live inside the uvicorn package tree
# and import these at their own top level, but list them explicitly too.
hiddenimports += ["uvloop", "httptools", "websockets", "watchfiles", "h11", "anyio", "yaml"]

a = Analysis(
    ["sidecar_main.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="market-api",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="market-api",
)
