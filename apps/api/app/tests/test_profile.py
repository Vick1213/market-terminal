"""M2.5 data-source profile gate + Tiingo provider tests.

pytest is not installed in the api venv (same situation as
app/ml/tests/test_leakage.py) — each check below is a plain function with
``assert`` and a ``__main__`` runner prints PASS/FAIL. Every function is
still named ``test_*`` and pytest-collectible unchanged once pytest is added.

Run:  cd apps/api && .venv/bin/python -m app.tests.test_profile

Covers:
  * the ``@gated`` decorator: no-ops in commercial profile, passes through
    unchanged in personal profile, rejects an unregistered source key.
  * every real RED call site named in docs/data-licensing-audit.md's M2.5
    gate list is actually wired to ``@gated`` (introspection via
    ``__gate_source__``, no DB/network fixtures needed).
  * the Tiingo provider parses a mocked response and never touches the
    network for a missing key or an unservable symbol (no live API calls).
"""

from __future__ import annotations

import asyncio

from app import profile
from app.ingest import tiingo


class _FakeSettings:
    def __init__(self, profile_value: str) -> None:
        self.profile = profile_value


def _with_profile(name: str):
    """Context-free monkeypatch: swap app.profile.get_settings, return the
    restore callable. (No pytest fixtures available — see module docstring.)"""
    orig = profile.get_settings
    profile.get_settings = lambda: _FakeSettings(name)  # type: ignore[assignment]
    return orig


def _restore(orig) -> None:
    profile.get_settings = orig


# --- check 1: the @gated decorator ------------------------------------------


def test_gated_noop_in_commercial() -> None:
    orig = _with_profile("commercial")
    try:
        calls = []

        @profile.gated("kalshi", default=lambda: "GATED")
        async def fn(x: int) -> str:
            calls.append(x)
            return "RAN"

        result = asyncio.run(fn(1))
        assert result == "GATED", result
        assert calls == [], "wrapped body must not run when gated"
    finally:
        _restore(orig)


def test_gated_passthrough_in_personal() -> None:
    orig = _with_profile("personal")
    try:
        calls = []

        @profile.gated("kalshi", default=lambda: "GATED")
        async def fn(x: int) -> str:
            calls.append(x)
            return "RAN"

        result = asyncio.run(fn(1))
        assert result == "RAN", result
        assert calls == [1], "personal profile must call through unchanged"
    finally:
        _restore(orig)


def test_gated_default_value_not_just_factory() -> None:
    """`default` may be a plain value (not just a zero-arg callable)."""
    orig = _with_profile("commercial")
    try:
        @profile.gated("aaii", default=None)
        async def fn() -> str:
            return "RAN"

        assert asyncio.run(fn()) is None
    finally:
        _restore(orig)


def test_gated_rejects_unknown_source_key() -> None:
    try:
        profile.gated("not-a-real-red-source")
    except KeyError:
        pass
    else:
        raise AssertionError("gated() must reject a source_key not in RED_SOURCES")


# --- check 2: every named RED call site is actually wired -------------------


def test_real_call_sites_wired_to_gate() -> None:
    from app.edge.calendar import CalendarPipeline
    from app.edge.congress import CongressPipeline
    from app.edge.gex import GexAdapter
    from app.ingest.kalshi import KalshiPipeline
    from app.ingest.macro import MacroPipeline
    from app.ingest.news import fetch_broad_rss, fetch_seekingalpha, fetch_yahoo
    from app.ingest.retail import RetailPipeline

    expected = {
        CongressPipeline.run: "congress_efd",
        fetch_seekingalpha: "seekingalpha",
        fetch_broad_rss: "broad_news_rss",
        RetailPipeline._stocktwits_symbol: "stocktwits",
        KalshiPipeline.run: "kalshi",
        MacroPipeline.run_cboe: "cboe",
        GexAdapter.run: "cboe",
        MacroPipeline.run_aaii: "aaii",
        CalendarPipeline._fmp_earnings_events: "fmp",
        # Yahoo-vendor gap closed: three more yfinance call sites (M2.5
        # follow-up, docs/data-licensing-audit.md "Implementation status").
        MacroPipeline.run_move: "yfinance",
        CalendarPipeline._yfinance_earnings_events: "yfinance",
        fetch_yahoo: "yfinance",
    }
    for func, source_key in expected.items():
        got = getattr(func, "__gate_source__", None)
        assert got == source_key, (
            f"{getattr(func, '__qualname__', func)} expected gate {source_key!r}, got {got!r}"
        )


def test_red_sources_registry_covers_the_gate_list() -> None:
    for key in (
        "yfinance", "congress_efd", "seekingalpha", "broad_news_rss",
        "stocktwits", "kalshi", "cboe", "aaii", "fmp",
    ):
        assert key in profile.RED_SOURCES, f"missing RED_SOURCES entry: {key}"
        spec = profile.RED_SOURCES[key]
        assert spec.reason, f"{key} needs a docs/data-licensing-audit.md citation"
        assert spec.hosts, f"{key} needs at least one host for source_health"


# --- check 3: Tiingo provider ------------------------------------------------


def test_tiingo_symbol_coverage() -> None:
    assert tiingo.tiingo_symbol("AAPL", "equity") == "AAPL"
    assert tiingo.tiingo_symbol("spy", "equity") == "SPY"  # uppercased
    assert tiingo.tiingo_symbol("BTC/USD", "crypto") is None
    assert tiingo.tiingo_symbol("XAU", "metal") is None
    assert tiingo.tiingo_symbol("XAG", "metal") is None
    assert tiingo.tiingo_symbol("^MOVE", "index") is None
    assert tiingo.tiingo_symbol("^VIX", "index") is None


def test_tiingo_parse_rows_mocked_payload() -> None:
    """Shape lifted from Tiingo's documented /tiingo/daily/<ticker>/prices
    response — never hits the live API, this is a pure function over a
    hand-built payload."""
    payload = [
        {
            "date": "2026-01-02T00:00:00.000Z",
            "open": 100.0, "high": 105.0, "low": 99.0, "close": 104.0,
            "volume": 1_000_000,
            "adjOpen": 99.5, "adjHigh": 104.5, "adjLow": 98.5, "adjClose": 103.5,
            "adjVolume": 999_000,
            "divCash": 0.0, "splitFactor": 1.0,
        },
        {
            # missing adj* fields -> falls back to raw OHLCV
            "date": "2026-01-05T00:00:00.000Z",
            "open": 104.0, "high": 108.0, "low": 103.0, "close": 107.0,
            "volume": 500_000,
        },
        {"date": "2026-01-06T00:00:00.000Z", "close": None},  # dropped: no close
        {"close": 999.0},  # dropped: no date
    ]
    rows = tiingo._parse_rows(payload, "AAPL", "equity")
    assert len(rows) == 2, rows

    source, symbol, asset_class, ts, o, h, lo, c, v = rows[0]
    assert source == "yahoo"  # namespace, not provenance — see prices.py
    assert symbol == "AAPL"
    assert asset_class == "equity"
    assert ts.year == 2026 and ts.month == 1 and ts.day == 2
    assert ts.hour == 0 and ts.minute == 0  # normalized to midnight
    assert (o, h, lo, c) == (99.5, 104.5, 98.5, 103.5)  # adjusted columns preferred
    assert v == 999_000

    _, _, _, _, o2, h2, lo2, c2, v2 = rows[1]
    assert (o2, h2, lo2, c2) == (104.0, 108.0, 103.0, 107.0)  # raw fallback
    assert v2 == 500_000


def test_tiingo_fetch_daily_bars_no_key_never_touches_network() -> None:
    from datetime import date

    # `http` is a bare sentinel with no get_json — an AttributeError would
    # prove the code tried to make a network call, which must never happen.
    result = asyncio.run(
        tiingo.fetch_daily_bars(object(), "", "AAPL", "equity", date(2026, 1, 1))
    )
    assert result == []


def test_tiingo_fetch_daily_bars_unsupported_symbol_never_touches_network() -> None:
    from datetime import date

    result = asyncio.run(
        tiingo.fetch_daily_bars(object(), "fake-key", "BTC/USD", "crypto", date(2026, 1, 1))
    )
    assert result == []


def _run_all() -> int:
    checks = [
        test_gated_noop_in_commercial,
        test_gated_passthrough_in_personal,
        test_gated_default_value_not_just_factory,
        test_gated_rejects_unknown_source_key,
        test_real_call_sites_wired_to_gate,
        test_red_sources_registry_covers_the_gate_list,
        test_tiingo_symbol_coverage,
        test_tiingo_parse_rows_mocked_payload,
        test_tiingo_fetch_daily_bars_no_key_never_touches_network,
        test_tiingo_fetch_daily_bars_unsupported_symbol_never_touches_network,
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
