"""M3: onboarding & settings store tests.

Same plain-assert style as test_profile.py — no pytest in the api venv.

Run:  cd apps/api && .venv/bin/python -m app.tests.test_settings_store

Covers:
  * atomic write (temp file + os.replace) and 0600 perms on the written file.
  * precedence store > env > default via apply_overlay + provenance_of.
  * unset-via-empty-string reverts a field to its env/default value.
  * secret masking (last 4 chars only; None when unset).
  * provenance reporting ("store" | "env" | "default").
  * whitelist rejection of an unknown field (set_fields raises ValueError).
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

from app import settings_store as ss


class _FakeSettings:
    """Minimal stand-in for app.config.Settings — only needs the whitelisted
    attributes apply_overlay/provenance_of actually touch, plus attribute
    assignment (a plain object supports both fine)."""

    def __init__(self, **kw) -> None:
        # Defaults mirror app/config.py's literal defaults for the fields
        # this test suite exercises (deliberately NOT importing the real
        # Settings class, so these tests never depend on the developer's own
        # apps/api/.env file).
        self.fred_api_key = kw.get("fred_api_key", "")
        self.tiingo_api_key = kw.get("tiingo_api_key", "")
        self.llm_provider = kw.get("llm_provider", "ollama")
        self.ntfy_topic = kw.get("ntfy_topic", "")
        self.ntfy_server = kw.get("ntfy_server", "https://ntfy.sh")


def _tmp_store() -> ss.SettingsStore:
    d = tempfile.mkdtemp(prefix="settings-store-test-")
    return ss.SettingsStore(path=Path(d) / "settings.json")


# --- check 1: atomic write + 0600 perms --------------------------------------


def test_save_is_atomic_and_0600() -> None:
    store = _tmp_store()
    store.save({"fred_api_key": "abc123"})
    assert store.path.exists()
    # No leftover temp files in the directory after a clean write.
    leftovers = [p for p in store.path.parent.iterdir() if p.name != store.path.name]
    assert leftovers == [], f"unexpected leftover files: {leftovers}"
    mode = stat.S_IMODE(os.stat(store.path).st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"
    assert store.load() == {"fred_api_key": "abc123"}


def test_save_overwrites_cleanly_and_stays_atomic() -> None:
    store = _tmp_store()
    store.save({"a": 1})
    store.save({"a": 2, "b": 3})
    assert store.load() == {"a": 2, "b": 3}
    leftovers = [p for p in store.path.parent.iterdir() if p.name != store.path.name]
    assert leftovers == [], f"unexpected leftover files: {leftovers}"


def test_load_missing_file_is_empty_dict() -> None:
    store = ss.SettingsStore(path=Path(tempfile.mkdtemp()) / "nope" / "settings.json")
    assert store.load() == {}


def test_load_corrupt_file_is_empty_dict() -> None:
    store = _tmp_store()
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{ not json")
    assert store.load() == {}


# --- check 2: whitelist enforcement ------------------------------------------


def test_set_fields_rejects_unknown_field() -> None:
    store = _tmp_store()
    try:
        store.set_fields({"fred_api_key": "x", "day_max_leverage": "3.0"})
    except ValueError as exc:
        assert "day_max_leverage" in str(exc)
    else:
        raise AssertionError("expected ValueError for a non-whitelisted field")
    # Nothing should have been written — the whole update is rejected.
    assert store.load() == {}


def test_set_fields_allows_the_onboarded_meta_flag() -> None:
    store = _tmp_store()
    store.set_fields({"onboarded": True})
    assert store.onboarded() is True


def test_field_spec_covers_every_group() -> None:
    groups = {spec.group for spec in ss.FIELD_SPEC.values()}
    assert groups == {"data", "broker", "ai", "alerts"}, groups
    for name in ("fred_api_key", "tiingo_api_key", "fmp_api_key", "finnhub_api_key"):
        assert ss.FIELD_SPEC[name].group == "data"
    for name in ("alpaca_key_id", "alpaca_secret_key", "broker_backend"):
        assert ss.FIELD_SPEC[name].group == "broker"
    assert ss.FIELD_SPEC["llm_api_key"].group == "ai"
    assert ss.FIELD_SPEC["ntfy_topic"].group == "alerts"


# --- check 3: precedence store > env > default + unset --------------------


def test_precedence_default_then_env_then_store() -> None:
    store = _tmp_store()
    settings = _FakeSettings(fred_api_key="")  # default: empty

    # Nothing in the store yet -> stays at the "env" value (here == default "").
    ss.apply_overlay(settings, store)
    assert settings.fred_api_key == ""
    assert ss.provenance_of(settings, store, "fred_api_key") == "default"

    # Simulate an env-provided key (as if MARKET_FRED_API_KEY were set) by
    # using a fresh settings object whose starting value already differs
    # from the class default before the FIRST overlay call ever sees it.
    settings_env = _FakeSettings(fred_api_key="env-provided-key")
    ss.apply_overlay(settings_env, store)
    assert settings_env.fred_api_key == "env-provided-key"
    assert ss.provenance_of(settings_env, store, "fred_api_key") == "env"

    # Now the store wins over env.
    store.set_fields({"fred_api_key": "store-key-wxyz"})
    ss.apply_overlay(settings_env, store)
    assert settings_env.fred_api_key == "store-key-wxyz"
    assert ss.provenance_of(settings_env, store, "fred_api_key") == "store"


def test_unset_via_empty_string_reverts_to_env_default() -> None:
    store = _tmp_store()
    settings = _FakeSettings(tiingo_api_key="original-env-key")
    ss.apply_overlay(settings, store)  # captures the env snapshot first

    store.set_fields({"tiingo_api_key": "overridden-store-key"})
    ss.apply_overlay(settings, store)
    assert settings.tiingo_api_key == "overridden-store-key"

    # PUT with "" unsets -> falls back to the original env value, LIVE
    # (no restart), because apply_overlay reverts unset fields to the
    # snapshot taken before the store ever touched this object.
    store.set_fields({"tiingo_api_key": ""})
    assert "tiingo_api_key" not in store.load()
    ss.apply_overlay(settings, store)
    assert settings.tiingo_api_key == "original-env-key"
    assert ss.provenance_of(settings, store, "tiingo_api_key") == "env"


# --- check 4: masking + describe_fields shape --------------------------------


def test_mask_value() -> None:
    assert ss.mask_value("") is None
    assert ss.mask_value(None) is None
    assert ss.mask_value("abcd") == "••••"  # <=4 chars: fully masked
    assert ss.mask_value("abcdefgh") == "••••efgh"


def test_describe_fields_masks_secrets_but_not_plain_fields() -> None:
    store = _tmp_store()
    settings = _FakeSettings(
        fred_api_key="supersecretkey1234",
        llm_provider="anthropic",
        ntfy_server="https://ntfy.sh",
    )
    ss.apply_overlay(settings, store)
    fields = ss.describe_fields(settings, store)

    assert fields["fred_api_key"]["secret"] is True
    assert fields["fred_api_key"]["value"] == "••••1234"

    assert fields["llm_provider"]["secret"] is False
    assert fields["llm_provider"]["value"] == "anthropic"  # not masked

    # Never leak the raw secret anywhere in the describe_fields output.
    import json

    dumped = json.dumps(fields)
    assert "supersecretkey1234" not in dumped


def test_settings_snapshot_shape() -> None:
    store = _tmp_store()
    settings = _FakeSettings()
    snap = ss.settings_snapshot(settings, store)
    assert snap["onboarded"] is False
    assert "fred_api_key" in snap["fields"]
    assert set(snap["fields"]["fred_api_key"]) == {"group", "label", "secret", "value", "provenance"}


def _run_all() -> int:
    checks = [
        test_save_is_atomic_and_0600,
        test_save_overwrites_cleanly_and_stays_atomic,
        test_load_missing_file_is_empty_dict,
        test_load_corrupt_file_is_empty_dict,
        test_set_fields_rejects_unknown_field,
        test_set_fields_allows_the_onboarded_meta_flag,
        test_field_spec_covers_every_group,
        test_precedence_default_then_env_then_store,
        test_unset_via_empty_string_reverts_to_env_default,
        test_mask_value,
        test_describe_fields_masks_secrets_but_not_plain_fields,
        test_settings_snapshot_shape,
    ]
    failed = 0
    for c in checks:
        try:
            c()
            print(f"PASS  {c.__name__}")
        except Exception as exc:  # noqa: BLE001 - test harness reporting
            failed += 1
            print(f"FAIL  {c.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(checks) - failed}/{len(checks)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
