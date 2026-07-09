"""M2.5 data-source licensing profile — see docs/data-licensing-audit.md.

The audit (2026-07-09) rated every external data source the backend touches
for use in a *paid* product. RED sources are fine for the owner's own
personal, BYO-key, single-account use, but ToS- or statute-prohibited once
the software is sold to other people. This module is the ONE place that
knows which sources are RED and what to do about it — every gated call site
imports from here instead of growing its own bespoke
``if settings.profile == "commercial"`` branch.

``MARKET_PROFILE=personal`` (default): zero behavior change, every source
runs exactly as before.

``MARKET_PROFILE=commercial``: RED sources cleanly no-op — the scheduled job
still fires, but the decorated function skips its body entirely (no network
call, no side effects, never raises) and returns an empty/None result. A
one-shot startup hook (``apply_profile_gates``, called from ``main.py``'s
lifespan) additionally pre-registers every RED host in ``source_health`` with
a distinct "disabled (profile)" status, so ``/api/health/sources`` explains
*why* a panel is empty instead of looking like every other unconfigured/never-
polled source.

yfinance (``app/ingest/prices.py``) is the one RED source enforced
differently for its *primary* daily-bar/live-quote role: it has a clean
drop-in replacement (Tiingo — see ``app/ingest/tiingo.py``), so
``prices.py``'s own provider-selection logic consults ``is_commercial()``
directly instead of using the ``gated`` decorator below (there is no useful
"no-op" for daily prices — commercial profile routes to Tiingo/Alpaca
instead of just going empty).

Three smaller Yahoo-vendor call sites have no such replacement and so ARE
wired to the plain ``gated("yfinance", ...)`` decorator below, same as any
other RED source (closes the gap formerly tracked here — see
docs/data-licensing-audit.md "Implementation status"):
``app/ingest/macro.py``'s ``MacroPipeline.run_move`` (``^MOVE`` bond-vol
index), ``app/edge/calendar.py``'s
``CalendarPipeline._yfinance_earnings_events`` (earnings-date fallback), and
``app/ingest/news.py``'s ``fetch_yahoo`` (per-ticker RSS). Commercial
profile: all three no-op cleanly (no network, never raises) — ``^MOVE`` has
no free substitute and is simply absent downstream (accepted, nothing reads
it assuming it exists), the earnings fallback degrades to whatever FMP
already covered that run, and Yahoo RSS just drops out of the news mix
(Finnhub/EDGAR/GDELT already cover it per that module's own docstring).
"""

from __future__ import annotations

import functools
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, TypeVar

from app.config import get_settings

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids a hard DB import
    from app.ingest.source_health import SourceHealthRegistry

log = logging.getLogger("market.profile")

_F = TypeVar("_F", bound=Callable[..., Any])

# source_health status strings used by apply_profile_gates() below. Kept as
# constants so the health router / any future UI copy can match on them.
DISABLED_STATUS = "disabled (profile)"
NEEDS_KEY_STATUS = "needs key"


@dataclass(frozen=True)
class RedSource:
    reason: str            # short ToS/statute citation -> docs/data-licensing-audit.md
    hosts: tuple[str, ...]  # outbound hosts to flag in source_health when gated


# Every RED-rated source from the audit that is hard-gated (no viable free
# replacement) in a commercial build. Keys are referenced by ``@gated(...)``
# below — grep for the key (or for ``__gate_source__``) to find every call
# site it guards.
RED_SOURCES: dict[str, RedSource] = {
    "yfinance": RedSource(
        reason=(
            "Yahoo ToS bars automated scraping/bots; yfinance is an unofficial "
            "TLS-impersonation workaround (data-licensing-audit.md #Yahoo Finance / "
            "yfinance). Primary role (app/ingest/prices.py daily bars/live quotes) "
            "is enforced via provider selection, not @gated — see this module's "
            "docstring. Three smaller call sites (macro.py ^MOVE, calendar.py "
            "earnings-date fallback, news.py per-ticker RSS) ARE wired directly to "
            "@gated('yfinance', ...)."
        ),
        hosts=(
            "query1.finance.yahoo.com", "query2.finance.yahoo.com",
            "feeds.finance.yahoo.com",
        ),
    ),
    "congress_efd": RedSource(
        reason=(
            "Ethics in Government Act makes commercial use of Financial Disclosure "
            "Reports UNLAWFUL, not just a ToS matter "
            "(data-licensing-audit.md #Senate eFD PTR tracker)"
        ),
        hosts=("efdsearch.senate.gov",),
    ),
    "seekingalpha": RedSource(
        reason=(
            "Seeking Alpha ToS: RSS use 'limited to personal, non-commercial use' "
            "(data-licensing-audit.md #Seeking Alpha RSS)"
        ),
        hosts=("seekingalpha.com",),
    ),
    "broad_news_rss": RedSource(
        reason=(
            "Dow Jones/MarketWatch + Investing.com: personal/non-commercial ToS "
            "(Investing.com explicitly bars 'an application'); CNBC unconfirmed, "
            "treated conservatively (data-licensing-audit.md #Dow Jones / MarketWatch "
            "RSS, #Investing.com RSS, #CNBC RSS)"
        ),
        hosts=("search.cnbc.com", "feeds.content.dowjones.io", "www.investing.com"),
    ),
    "stocktwits": RedSource(
        reason=(
            "StockTwits ToS: personal/non-commercial use only, and not currently "
            "accepting new developer API registrations at all "
            "(data-licensing-audit.md #StockTwits)"
        ),
        hosts=("api.stocktwits.com",),
    ),
    "kalshi": RedSource(
        reason=(
            "Kalshi Data ToS: personal/non-business use only, bans AI/ML use, and "
            "forbids sharing without Kalshi's written permission "
            "(data-licensing-audit.md #Kalshi)"
        ),
        hosts=("api.elections.kalshi.com",),
    ),
    "cboe": RedSource(
        reason=(
            "Cboe Market Data Policy: auto-extraction 'strictly prohibited', "
            "redistribution requires a signed Data Agreement + fees; FRED's VIXCLS "
            "is the surviving free VIX substitute (data-licensing-audit.md #CBOE)"
        ),
        hosts=("cdn.cboe.com", "www.cboe.com"),
    ),
    "aaii": RedSource(
        reason=(
            "AAII ToS: 'no part of the contents...may be copied or forwarded to "
            "anyone else' (data-licensing-audit.md #AAII)"
        ),
        hosts=("www.aaii.com",),
    ),
    "fmp": RedSource(
        reason=(
            "FMP free tier bars 'Commercial Use' and requires a Data Display & "
            "Licensing Agreement to show FMP data in an app "
            "(data-licensing-audit.md #FMP)"
        ),
        hosts=("financialmodelingprep.com",),
    ),
}


def is_commercial() -> bool:
    return get_settings().profile == "commercial"


def gated(source_key: str, default: Any = None) -> Callable[[_F], _F]:
    """Decorator for a RED-rated async fetcher/pipeline entrypoint.

    Personal profile (default): calls through unchanged, zero overhead.
    Commercial profile: skips the wrapped call entirely (no network request,
    no side effects, never raises) and returns ``default`` — called first if
    it is callable (so mutable defaults like ``list``/``dict`` don't leak
    shared state between calls), otherwise returned as-is.

    ``source_key`` must already be registered in ``RED_SOURCES`` — this is a
    deliberate footgun-guard so a gate can't silently reference a source with
    no citation/host mapping.
    """
    if source_key not in RED_SOURCES:
        raise KeyError(
            f"{source_key!r} is not registered in app.profile.RED_SOURCES — "
            "add it there first (with its docs/data-licensing-audit.md citation)"
        )

    def decorator(func: _F) -> _F:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            if is_commercial():
                log.debug(
                    "%s gated off (commercial profile, source=%s): %s",
                    getattr(func, "__qualname__", func), source_key,
                    RED_SOURCES[source_key].reason,
                )
                return default() if callable(default) else default
            return await func(*args, **kwargs)

        wrapper.__gate_source__ = source_key  # introspection hook for tests/grep
        return wrapper  # type: ignore[return-value]

    return decorator


def apply_profile_gates(settings, source_health: "SourceHealthRegistry") -> None:
    """One-shot startup hook (called from ``main.py``'s lifespan).

    Commercial profile: pre-registers every RED host in ``source_health`` as
    "disabled (profile)" so ``/api/health/sources`` explains empty panels
    from the very first request, instead of waiting for a scheduled run that
    will never make a network call under the gate. Also flags a missing
    Tiingo key (the yfinance replacement — see app/ingest/tiingo.py) with a
    distinct "needs key" status, since without it commercial-profile daily
    prices have no primary provider at all (yfinance is gated off).

    Personal profile: does nothing — zero behavior change.
    """
    if settings.profile != "commercial":
        return
    for key, spec in RED_SOURCES.items():
        for host in spec.hosts:
            source_health.record_disabled(host, DISABLED_STATUS, spec.reason)
    log.info(
        "commercial profile active: gated %d RED source(s) — %s",
        len(RED_SOURCES), ", ".join(sorted(RED_SOURCES)),
    )
    if not settings.tiingo_api_key:
        log.warning(
            "commercial profile: MARKET_TIINGO_API_KEY not set — yfinance is "
            "gated off, so daily-bar history/live quotes will be missing/stale "
            "for any symbol Alpaca doesn't also cover (see app/ingest/tiingo.py)"
        )
        source_health.record_disabled(
            "api.tiingo.com", NEEDS_KEY_STATUS,
            "yfinance is gated off in commercial profile; set MARKET_TIINGO_API_KEY "
            "to restore daily-bar history/live quotes",
        )
