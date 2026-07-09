"""BYO API-key / broker / LLM settings store — M3 onboarding & settings UI.

Everything in ``app/config.py`` is env-driven (``MARKET_*`` / apps/api/.env)
by design — that path keeps working completely unchanged for developers and
anyone who prefers a text file. This module adds an OPTIONAL layer on top: a
small, whitelisted set of BYO credential / provider-endpoint fields the
desktop settings UI can read and write without ever touching a shell or a
.env file.

Precedence when resolving the effective value of any whitelisted field::

    store (apps/api/data/settings.json) > env (.env / os.environ) > default

The store is a single small JSON file — deliberately NOT inside
``settings.data_dir`` (the DuckDB/SQLite/HTTP-cache directory from
app/config.py) so a future "reset my data" wipe of that directory can never
nuke API keys, and so the desktop packaging story never has to teach the
settings UI about DuckDB just to manage credentials. File mode 0600, atomic
writes (temp file in the same directory + ``os.replace``) so a crash mid-
write can never corrupt it or leave a partially-written secret on disk.

Only fields in ``FIELD_SPEC`` below may ever be read or written through this
module — every internal tuning knob (poll cadences, guardrail %, hard safety
gates like ``bot_allow_live_trading`` / ``ibkr_allow_live``) stays env-only
on purpose. This is a deliberate footgun-guard: the whitelist is enforced in
``SettingsStore.set_fields`` itself, not just at the router layer, so no
other caller can sneak a non-whitelisted field into the store.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("market.settings_store")

# apps/api/app/settings_store.py -> parents: [0]=app [1]=api
_API_ROOT = Path(__file__).resolve().parents[1]


def _resolve_default_settings_path() -> Path:
    """``MARKET_SETTINGS_PATH`` env override wins when set (the PyInstaller
    desktop sidecar, apps/api/sidecar_main.py, points this at a per-user
    app-data directory before any ``app.*`` module is imported); otherwise
    the long-standing dev default, apps/api/data/settings.json.

    Resolved fresh on every call (not cached at import time) so a
    ``SettingsStore()`` constructed after the env var is set — sidecar
    startup, or a test that pokes ``os.environ`` — picks it up without
    needing a module reload."""
    override = os.environ.get("MARKET_SETTINGS_PATH")
    if override:
        return Path(override)
    return _API_ROOT / "data" / "settings.json"


DEFAULT_SETTINGS_PATH = _resolve_default_settings_path()


@dataclass(frozen=True)
class FieldSpec:
    group: str  # "data" | "broker" | "ai" | "alerts" -- the settings-modal groups
    label: str
    secret: bool = True  # secret fields are masked on GET (last 4 chars only)


# The whitelist. Every BYO credential / provider-endpoint field the settings
# UI may touch — cross-check app/config.py's own field list before adding to
# this; anything NOT here is rejected by SettingsStore.set_fields with a
# ValueError (the router turns that into an HTTP 400).
FIELD_SPEC: dict[str, FieldSpec] = {
    # --- Data providers ---
    "fred_api_key": FieldSpec("data", "FRED API key"),
    "tiingo_api_key": FieldSpec("data", "Tiingo API key"),
    "fmp_api_key": FieldSpec("data", "FMP API key"),
    "finnhub_api_key": FieldSpec("data", "Finnhub API key"),
    # --- Broker (Alpaca paper trading + Interactive Brokers) ---
    "broker_backend": FieldSpec("broker", "Broker backend (alpaca | ibkr)", secret=False),
    "alpaca_key_id": FieldSpec("broker", "Alpaca data key ID"),
    "alpaca_secret_key": FieldSpec("broker", "Alpaca data secret key"),
    "alpaca_paper_key_id": FieldSpec("broker", "Alpaca paper key ID"),
    "alpaca_paper_secret_key": FieldSpec("broker", "Alpaca paper secret key"),
    "alpaca_trading_base_url": FieldSpec("broker", "Alpaca trading base URL", secret=False),
    "ibkr_base_url": FieldSpec("broker", "IBKR Client Portal Gateway URL", secret=False),
    "ibkr_account_id": FieldSpec("broker", "IBKR account id", secret=False),
    # --- AI / LLM narratives ---
    "llm_provider": FieldSpec(
        "ai", "LLM provider (ollama | openai | deepseek | anthropic)", secret=False
    ),
    "llm_api_key": FieldSpec("ai", "LLM API key"),
    "llm_model": FieldSpec("ai", "LLM model override", secret=False),
    "llm_base_url": FieldSpec("ai", "LLM base URL override", secret=False),
    "ollama_url": FieldSpec("ai", "Ollama URL", secret=False),
    "ollama_model": FieldSpec("ai", "Ollama model", secret=False),
    # --- Alerts (ntfy push) ---
    "ntfy_topic": FieldSpec("alerts", "ntfy topic"),
    "ntfy_server": FieldSpec("alerts", "ntfy server", secret=False),
}

# Store-only meta flag(s): live alongside the whitelisted fields in the JSON
# file but have no app/config.py Settings counterpart, so there's nothing to
# overlay onto the settings singleton for these.
_META_FIELDS = {"onboarded"}

_SNAPSHOT_ATTR = "_settings_store_env_snapshot"


class SettingsStore:
    """Reads/writes apps/api/data/settings.json. A missing or corrupt file
    behaves as ``{}`` — the store is always optional; a fresh checkout with
    no file behaves exactly like the pre-M3 env-only world."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else _resolve_default_settings_path()

    def load(self) -> dict[str, Any]:
        try:
            raw = self.path.read_text()
        except FileNotFoundError:
            return {}
        except OSError as exc:
            log.warning("settings store unreadable (%s): %s", self.path, exc)
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            log.warning("settings store corrupt (%s): %s", self.path, exc)
            return {}
        return data if isinstance(data, dict) else {}

    def save(self, data: dict[str, Any]) -> None:
        """Atomic write: temp file in the same directory + ``os.replace``, so
        a crash/kill mid-write can never leave a truncated or partially-
        written settings.json. Mode 0600 — the file holds live API keys."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=".settings-", suffix=".tmp", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2, sort_keys=True)
                f.write("\n")
            os.chmod(tmp_name, stat.S_IRUSR | stat.S_IWUSR)  # 0600 before it's visible
            os.replace(tmp_name, self.path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def onboarded(self) -> bool:
        return bool(self.load().get("onboarded", False))

    def set_fields(self, updates: dict[str, Any]) -> dict[str, Any]:
        """Partial update. An empty-string value UNSETS a field (removed
        from the store — the next overlay falls back to env/default).
        Rejects any key outside the whitelist with ``ValueError`` naming
        every offending key. Returns the merged store dict that was written.
        """
        allowed = FIELD_SPEC.keys() | _META_FIELDS
        unknown = sorted(k for k in updates if k not in allowed)
        if unknown:
            raise ValueError(f"not a whitelisted settings field: {', '.join(unknown)}")
        data = self.load()
        for key, value in updates.items():
            if value == "":
                data.pop(key, None)
            else:
                data[key] = value
        self.save(data)
        return data


_default_store: SettingsStore | None = None


def get_settings_store() -> SettingsStore:
    """Process-wide default store (apps/api/data/settings.json). Tests
    should construct their own ``SettingsStore(path=...)`` instead of using
    this singleton."""
    global _default_store
    if _default_store is None:
        _default_store = SettingsStore()
    return _default_store


def _env_snapshot(settings: Any) -> dict[str, Any]:
    """The env/default value of every whitelisted field, captured the FIRST
    time ``apply_overlay``/``provenance_of`` ever sees this ``settings``
    object — i.e. before the store has had any chance to mutate it. Needed
    so an UNSET (PUT with ``""``) can revert the live singleton to its env
    value instead of getting stuck at the last store value, and so
    provenance can tell "env" apart from "default" after the overlay has
    already run.
    """
    snap = getattr(settings, _SNAPSHOT_ATTR, None)
    if snap is None:
        snap = {name: getattr(settings, name) for name in FIELD_SPEC if hasattr(settings, name)}
        setattr(settings, _SNAPSHOT_ATTR, snap)
    return snap


def apply_overlay(settings: Any, store: SettingsStore | None = None) -> None:
    """Mutate ``settings`` in place: every whitelisted field present (and
    non-empty) in the store wins; every other whitelisted field is reset to
    its original env/default value (so a just-cleared field takes effect
    immediately, not just on the next restart).

    Call once at startup — before any pipeline/service is constructed off of
    ``settings`` — and again after every ``PUT /api/settings`` so a saved key
    takes effect without a process restart wherever the reader consults
    ``settings.<field>`` live (some readers cache a value at construction
    time and would still need a restart; that's an inherent limitation of a
    single-process app, not something this function can fix).

    Fields not in ``FIELD_SPEC`` are never touched.
    """
    store = store or get_settings_store()
    snapshot = _env_snapshot(settings)
    data = store.load()
    for name in FIELD_SPEC:
        if not hasattr(settings, name):
            continue  # whitelist entry with no live Settings attribute (shouldn't happen)
        stored = data.get(name)
        if stored not in (None, ""):
            setattr(settings, name, stored)
        else:
            setattr(settings, name, snapshot[name])


def _class_default(name: str) -> Any:
    from app.config import Settings

    field = Settings.model_fields.get(name)
    return field.default if field is not None else None


def provenance_of(settings: Any, store: SettingsStore, name: str) -> str:
    """"store" | "env" | "default" for one whitelisted field."""
    data = store.load()
    if data.get(name) not in (None, ""):
        return "store"
    snapshot = _env_snapshot(settings)
    if name in snapshot and snapshot[name] != _class_default(name):
        return "env"
    return "default"


def mask_value(value: Any) -> str | None:
    """Show only the last 4 characters (``"••••abcd"``); ``None`` if unset.
    Values of 4 chars or fewer are masked entirely (too short to safely
    reveal any of it)."""
    if not value:
        return None
    s = str(value)
    if len(s) <= 4:
        return "•" * len(s)
    return "••••" + s[-4:]


def describe_fields(settings: Any, store: SettingsStore | None = None) -> dict[str, dict]:
    """Every whitelisted field: group/label/secret + the masked-or-plain
    current value + its provenance. Powers ``GET /api/settings``."""
    store = store or get_settings_store()
    out: dict[str, dict] = {}
    for name, spec in FIELD_SPEC.items():
        raw = getattr(settings, name, "")
        value = mask_value(raw) if spec.secret else (raw if raw not in (None, "") else None)
        out[name] = {
            "group": spec.group,
            "label": spec.label,
            "secret": spec.secret,
            "value": value,
            "provenance": provenance_of(settings, store, name),
        }
    return out


def settings_snapshot(settings: Any, store: SettingsStore | None = None) -> dict:
    """The full GET /api/settings response shape."""
    store = store or get_settings_store()
    return {"onboarded": store.onboarded(), "fields": describe_fields(settings, store)}
