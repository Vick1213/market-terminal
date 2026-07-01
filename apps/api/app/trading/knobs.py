"""Runtime tunable knobs + the learning-loop suggestion ledger.

The day trader's parameters live in ``settings`` (env), read-only by design. This
module adds a NARROW, BOUNDED runtime-override layer on top: the learning loop
may propose changes to a *whitelisted* set of knobs, the user accepts them, and
the accepted value is written to ``bot_param_overrides`` — which the day trader
reads on top of its env defaults (see ``daytrader._knob``). Anything not in
``PARAM_SPEC`` cannot be changed at runtime, and every value is clamped to a safe
range, so the loop can never drive a parameter somewhere dangerous.

The suggestion ledger (``bot_suggestions``) records every proposal, its
lifecycle (proposed → accepted/rejected/reverted/superseded), and a before/after
metric so the loop can GRADE its own past advice — the feedback loop that turns
the daily report into something that actually learns.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

log = logging.getLogger("market.trading.knobs")


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- the whitelist: only these knobs are runtime-tunable, within these bounds --
# Each spec: type, [lo, hi] bounds, human label. ``attr`` defaults to the key
# (the settings attribute name). Bounds mirror the _clamp() ranges the review
# loop already uses, so an accepted suggestion is always inside them.
PARAM_SPEC: dict[str, dict] = {
    "day_stop_sigma":       {"type": "float", "lo": 0.5, "hi": 3.0,  "label": "Stop distance (σ)"},
    "day_tp_sigma":         {"type": "float", "lo": 1.5, "hi": 6.0,  "label": "Take-profit reach (σ)"},
    "day_reversion_z":      {"type": "float", "lo": 1.5, "hi": 3.5,  "label": "Reversion z-stretch"},
    "day_momentum_min_pct": {"type": "float", "lo": 0.20, "hi": 1.0, "label": "Momentum min move %"},
    "day_min_signal":       {"type": "float", "lo": 0.5, "hi": 3.0,  "label": "Min signal strength"},
    "day_stop_pct":         {"type": "float", "lo": 0.3, "hi": 2.0,  "label": "Fallback stop %"},
    "day_stop_floor_pct":   {"type": "float", "lo": 0.1, "hi": 1.5,  "label": "Min stop/trail distance %"},
    # NB: day_alloc_max_pct is intentionally NOT here — it's read once at startup
    # by the optimizer, so a runtime override would be a no-op. It stays a
    # display-only suggestion (needs an env change + restart to take effect).
    "day_net_beta_band_pct": {"type": "float", "lo": 0.5, "hi": 4.0, "label": "Hedge rebalance band %"},
    "day_max_new_positions": {"type": "int",  "lo": 1,   "hi": 8,    "label": "Max new positions/tick"},
    "day_respect_strategist": {"type": "bool", "label": "Respect strategist avoid-list"},
}


class KnobError(ValueError):
    """A runtime-knob change was rejected (unknown param / bad value)."""


def coerce(param: str, raw) -> object:
    """Validate + type-coerce + clamp a value for ``param``. Raises KnobError for
    an unknown param or an uncoercible value."""
    spec = PARAM_SPEC.get(param)
    if spec is None:
        raise KnobError(f"{param!r} is not a runtime-tunable knob")
    t = spec["type"]
    if t == "bool":
        if isinstance(raw, bool):
            return raw
        s = str(raw).strip().lower()
        if s in ("true", "1", "yes", "on"):
            return True
        if s in ("false", "0", "no", "off"):
            return False
        raise KnobError(f"{param}: {raw!r} is not a boolean")
    v = _f(raw)
    if v is None:
        raise KnobError(f"{param}: {raw!r} is not a number")
    lo, hi = spec["lo"], spec["hi"]
    v = max(lo, min(hi, v))
    if t == "int":
        return int(round(v))
    return round(v, 4)


def _default(settings, param: str):
    spec = PARAM_SPEC[param]
    return getattr(settings, spec.get("attr", param), None)


# --- override CRUD ---------------------------------------------------------- #
def list_overrides(sqlite) -> dict[str, dict]:
    """{param: {value(typed), updated_at, source}} for every live override."""
    out: dict[str, dict] = {}
    try:
        rows = sqlite.fetchall("SELECT param, value, updated_at, source FROM bot_param_overrides")
    except Exception:
        return out
    for r in rows:
        p = r["param"]
        if p not in PARAM_SPEC:
            continue
        try:
            out[p] = {"value": coerce(p, r["value"]), "updated_at": r["updated_at"],
                      "source": r["source"]}
        except KnobError:
            continue
    return out


def effective_value(sqlite, settings, param: str):
    """The live value for ``param``: override if present + valid, else env default."""
    ov = list_overrides(sqlite).get(param)
    return ov["value"] if ov else _default(settings, param)


def effective_params(sqlite, settings) -> dict[str, object]:
    """Live value for every whitelisted knob (override ?? env default)."""
    ov = list_overrides(sqlite)
    return {p: (ov[p]["value"] if p in ov else _default(settings, p)) for p in PARAM_SPEC}


def set_override(sqlite, param: str, value, source: str = "manual"):
    """Write (or replace) a runtime override. Returns the coerced value."""
    val = coerce(param, value)
    stored = "true" if val is True else "false" if val is False else str(val)
    sqlite.execute(
        "INSERT OR REPLACE INTO bot_param_overrides (param, value, updated_at, source) "
        "VALUES (?,?,?,?)", [param, stored, _now(), source])
    log.info("knob override %s -> %s (%s)", param, val, source)
    return val


def clear_override(sqlite, param: str) -> None:
    sqlite.execute("DELETE FROM bot_param_overrides WHERE param = ?", [param])
    log.info("knob override %s cleared (revert to default)", param)


def knobs_view(sqlite, settings) -> list[dict]:
    """UI-facing list: every knob with its default, live value, and bounds."""
    ov = list_overrides(sqlite)
    out = []
    for p, spec in PARAM_SPEC.items():
        o = ov.get(p)
        out.append({
            "param": p, "label": spec["label"], "type": spec["type"],
            "min": spec.get("lo"), "max": spec.get("hi"),
            "default": _default(settings, p),
            "value": o["value"] if o else _default(settings, p),
            "overridden": o is not None,
            "source": o["source"] if o else None,
            "updated_at": o["updated_at"] if o else None,
        })
    return out


# --- suggestion ledger ------------------------------------------------------ #
def _val_str(v) -> str:
    return "true" if v is True else "false" if v is False else str(v)


def record_suggestions(sqlite, sleeve: str, review_date: str,
                       suggestions: list[dict], *, metric_before: float | None = None) -> int:
    """Persist a review's suggestions to the ledger. Only whitelisted params are
    stored (others are display-only). Any prior UNDECIDED ('proposed') row for the
    same param+sleeve is marked 'superseded' so only the latest proposal is live.
    Returns the number of new rows inserted."""
    inserted = 0
    for s in suggestions:
        param = s.get("param")
        if param not in PARAM_SPEC:
            continue  # not a runtime knob — leave it as advisory display only
        # Supersede older still-pending proposals for this param.
        sqlite.execute(
            "UPDATE bot_suggestions SET status='superseded', decided_at=? "
            "WHERE sleeve=? AND param=? AND status='proposed'",
            [_now(), sleeve, param])
        sqlite.execute(
            "INSERT INTO bot_suggestions (created_at, sleeve, review_date, param, "
            "current_value, proposed_value, rationale, confidence, n_sample, actionable, "
            "status, metric_before) VALUES (?,?,?,?,?,?,?,?,?,?, 'proposed', ?)",
            [_now(), sleeve, review_date, param, _val_str(s.get("current")),
             _val_str(s.get("proposed")), s.get("rationale"), s.get("confidence"),
             s.get("n"), 1 if s.get("actionable") else 0, metric_before],
        )
        inserted += 1
    return inserted


def list_suggestions(sqlite, *, sleeve: str | None = None, status: str | None = None,
                     limit: int = 100) -> list[dict]:
    where, params = [], []
    if sleeve:
        where.append("sleeve = ?"); params.append(sleeve)
    if status:
        where.append("status = ?"); params.append(status)
    sql = "SELECT * FROM bot_suggestions"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    try:
        return [dict(r) for r in sqlite.fetchall(sql, params)]
    except Exception:
        return []


def get_suggestion(sqlite, sid: int) -> dict | None:
    row = sqlite.fetchone("SELECT * FROM bot_suggestions WHERE id = ?", [sid])
    return dict(row) if row else None


def accept_suggestion(sqlite, settings, sid: int) -> dict:
    """Accept a pending suggestion: write the override (coerced+clamped), mark it
    accepted, and stamp the applied value. The change takes effect on the next
    day-trader tick."""
    s = get_suggestion(sqlite, sid)
    if s is None:
        return {"ok": False, "detail": f"suggestion {sid} not found"}
    if s["status"] != "proposed":
        return {"ok": False, "detail": f"suggestion {sid} is '{s['status']}', not pending"}
    param = s["param"]
    if param not in PARAM_SPEC:
        return {"ok": False, "detail": f"{param} is not a runtime-tunable knob"}
    try:
        applied = set_override(sqlite, param, s["proposed_value"], source=f"suggestion:{sid}")
    except KnobError as exc:
        return {"ok": False, "detail": str(exc)}
    sqlite.execute(
        "UPDATE bot_suggestions SET status='accepted', decided_at=?, applied_value=? WHERE id=?",
        [_now(), _val_str(applied), sid])
    return {"ok": True, "param": param, "applied_value": applied,
            "detail": f"{param} → {applied} (live on next day-trader tick)"}


def reject_suggestion(sqlite, sid: int) -> dict:
    s = get_suggestion(sqlite, sid)
    if s is None:
        return {"ok": False, "detail": f"suggestion {sid} not found"}
    if s["status"] != "proposed":
        return {"ok": False, "detail": f"suggestion {sid} is '{s['status']}', not pending"}
    sqlite.execute("UPDATE bot_suggestions SET status='rejected', decided_at=? WHERE id=?",
                   [_now(), sid])
    return {"ok": True, "detail": f"suggestion {sid} rejected"}


def revert_accepted(sqlite, sid: int) -> dict:
    """Undo an accepted suggestion: drop its override, mark it reverted."""
    s = get_suggestion(sqlite, sid)
    if s is None:
        return {"ok": False, "detail": f"suggestion {sid} not found"}
    if s["status"] != "accepted":
        return {"ok": False, "detail": f"suggestion {sid} is '{s['status']}', not accepted"}
    clear_override(sqlite, s["param"])
    sqlite.execute("UPDATE bot_suggestions SET status='reverted', decided_at=? WHERE id=?",
                   [_now(), sid])
    return {"ok": True, "detail": f"{s['param']} override cleared; suggestion {sid} reverted"}


def grade_accepted(sqlite, sleeve: str, metric_after_by_date) -> int:
    """Grade accepted suggestions: fill ``metric_after`` from the caller-supplied
    map {decided_date -> expectancy $/trade AFTER that date}. Lets the next review
    say 'accepting X moved expectancy from before→after'. Returns rows graded."""
    graded = 0
    for s in list_suggestions(sqlite, sleeve=sleeve, status="accepted"):
        if s.get("metric_after") is not None or not s.get("decided_at"):
            continue
        after = metric_after_by_date(s["decided_at"])
        if after is None:
            continue
        sqlite.execute("UPDATE bot_suggestions SET metric_after=?, graded_at=? WHERE id=?",
                       [after, _now(), s["id"]])
        graded += 1
    return graded
