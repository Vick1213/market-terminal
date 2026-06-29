"""Volatility baselines — the integrity check the §12 vol result was missing.

Our cross-sectional GBM forecasts forward realised vol at IC ~0.46 (h=5) / 0.61
(h=21). But vol is PERSISTENT: high-vol names stay high-vol, so a naive
"rank by trailing vol" predictor may already score most of that IC. Until we put
a naive-persistence and a HAR baseline next to the GBM, the GBM "skill" is
unproven (Audrino & Chassot 2024, "HARd to Beat": a properly tuned HAR beats
Lasso/RF/GBM/NN on 1,445 stocks). This module builds those baselines and scores
them with the IDENTICAL target, cross-sectional IC, and date-purged walk-forward
the GBM used — so the comparison is apples-to-apples.

Baselines (predicting `labels.forward_realized_vol(close, h)` = std of next-h log
returns, ranked cross-sectionally across the 147 names each day):
  naive_cc  — trailing 21d close-to-close realised vol (parameter-free).
  naive_gk  — trailing 21d mean Garman-Klass daily vol (uses OHLC, lower variance).
  HAR       — panel OLS on [log RV_d, log RV_w, log RV_m] (Corsi 2009) over
              Garman-Klass daily vol, refit per walk-forward fold.

Garman-Klass daily variance from OHLC is a ~5–8× more efficient vol estimator than
close-to-close, which is the right input for HAR when no intraday data exists.

Run:  cd apps/api && .venv/bin/python -m app.ml.vol_baselines --horizons 5,21
"""
from __future__ import annotations

import argparse
import warnings
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from app.ml import labels
from app.ml.cross_section import DateWalkForward, cross_sectional_ic

# Reference GBM fwd_vol cross-sectional IC from the head-to-head (hand-features,
# same harness): the number the baselines must be measured against.
GBM_REF = {5: 0.460, 21: 0.610}
_LN2 = np.log(2.0)


def _gk_daily_vol(o, h, l, c) -> pd.Series:
    """Garman-Klass daily volatility (sqrt of the GK variance estimator)."""
    hl = np.log(h / l) ** 2
    co = np.log(c / o) ** 2
    var = 0.5 * hl - (2.0 * _LN2 - 1.0) * co
    var = var.clip(lower=1e-8)            # GK var can go slightly negative on doji days
    return np.sqrt(var)                    # daily vol units (~ std of daily log-ret)


def _load_ohlc(con, sym):
    rows = con.execute(
        "SELECT ts, open, high, low, close FROM ts_price WHERE symbol=? AND source='yahoo' "
        "AND open IS NOT NULL AND high IS NOT NULL AND low IS NOT NULL AND close IS NOT NULL "
        "AND low>0 AND open>0 ORDER BY ts", [sym]).fetchall()
    if not rows:
        return None
    idx = pd.to_datetime([r[0] for r in rows]).normalize()
    df = pd.DataFrame(rows, columns=["ts", "o", "h", "l", "c"], index=idx).drop(columns="ts")
    return df.astype(float)


def _build(con, symbols, h, start):
    """Per-(date,asset): HAR features + naive predictors + the forward-vol target."""
    frames = []
    for sym in symbols:
        d = _load_ohlc(con, sym)
        if d is None or len(d) < 300:
            continue
        gk = _gk_daily_vol(d.o, d.h, d.l, d.c)                  # daily GK vol
        f = pd.DataFrame(index=d.index)
        f["lrv_d"] = np.log(gk.clip(lower=1e-6))                # HAR daily comp
        f["lrv_w"] = np.log(gk.rolling(5, min_periods=3).mean().clip(lower=1e-6))
        f["lrv_m"] = np.log(gk.rolling(22, min_periods=11).mean().clip(lower=1e-6))
        f["naive_gk"] = gk.rolling(21, min_periods=11).mean()  # trailing GK vol
        f["naive_cc"] = labels.realized_vol(d.c, 21)           # trailing close-close vol
        f["target"] = labels.forward_realized_vol(d.c, h)      # SAME target as GBM
        f["asset"] = sym
        f.index.name = "date"
        frames.append(f.reset_index().set_index(["date", "asset"]))
    m = pd.concat(frames).sort_index()
    return m[m.index.get_level_values("date") >= pd.Timestamp(start)]


def _naive_ic(m, col):
    """Parameter-free predictor → pooled cross-sectional IC (no fit, so the whole
    series is OOS)."""
    sub = m[[col, "target"]].dropna()
    d = sub.index.get_level_values("date").to_numpy()
    return cross_sectional_ic(d, sub[col].to_numpy(), sub["target"].to_numpy())


def _har_ic(m, h):
    """Panel HAR: OLS on [lrv_d,lrv_w,lrv_m] refit per date-purged fold."""
    cols = ["lrv_d", "lrv_w", "lrv_m"]
    sub = m[cols + ["target"]].dropna()
    X = sub[cols].to_numpy(float)
    y = sub["target"].to_numpy(float)
    dates = sub.index.get_level_values("date").to_numpy()
    splitter = DateWalkForward(n_splits=6, min_train_days=504, embargo_days=h, horizon=h)
    P, T, D = [], [], []
    for tr, te in splitter.split(dates):
        if tr.sum() < 200 or te.sum() < 50:
            continue
        Xtr = np.c_[np.ones(tr.sum()), X[tr]]
        beta, *_ = np.linalg.lstsq(Xtr, y[tr], rcond=None)     # plain OLS
        pred = np.c_[np.ones(te.sum()), X[te]] @ beta
        P.append(pred); T.append(y[te]); D.append(dates[te])
    if not P:
        return None
    return cross_sectional_ic(np.concatenate(D), np.concatenate(P), np.concatenate(T))


def run(horizons=(5, 21), start="2006-01-01", uni_db=None):
    warnings.filterwarnings("ignore"); np.seterr(invalid="ignore", divide="ignore")
    repo = Path(__file__).resolve().parents[4]
    uni_db = Path(uni_db) if uni_db else repo / "data" / "ml" / "universe.duckdb"
    con = duckdb.connect(str(uni_db), read_only=True)
    symbols = [r[0] for r in con.execute(
        "SELECT DISTINCT symbol FROM ts_price WHERE source='yahoo'").fetchall()]
    print(f"=== VOL BASELINES vs GBM ===  {len(symbols)} names, start {start}", flush=True)
    print("Does the cross-sectional GBM actually beat naive vol-persistence / HAR?\n", flush=True)

    for h in horizons:
        m = _build(con, symbols, h, start)
        ncc = _naive_ic(m, "naive_cc")
        ngk = _naive_ic(m, "naive_gk")
        har = _har_ic(m, h)
        gbm = GBM_REF.get(h)
        print(f"----- h={h}  (forward realised vol, cross-sectional IC over {len(symbols)} names) -----")
        print(f"  {'model':18s} {'IC':>8} {'IC-t':>7} {'days':>5} {'hit%':>5}")
        for name, r in [("naive_cc (cc rvol)", ncc), ("naive_gk (GK rvol)", ngk),
                        ("HAR (Corsi/GK)", har)]:
            if r:
                print(f"  {name:18s} {r['ic']:>8.4f} {r['ic_t']:>7.1f} "
                      f"{r['n_days']:>5} {100*r['hit']:>4.0f}%")
        print(f"  {'GBM (56 feat) ref':18s} {gbm:>8.4f}   (hand-features, head-to-head)")
        best_base = max([x for x in (ncc, ngk, har) if x], key=lambda r: r["ic"])
        lift = gbm - best_base["ic"] if gbm else float("nan")
        verdict = ("GBM BEATS baselines — real skill beyond persistence" if lift > 0.03
                   else "GBM ≈ baselines — vol 'edge' is mostly persistence" if lift > -0.01
                   else "GBM WORSE than a baseline (!)")
        print(f"  → best baseline IC {best_base['ic']:.4f}; GBM lift {lift:+.4f}  ⇒ {verdict}\n")
    con.close()


def _main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizons", default="5,21")
    ap.add_argument("--start", default="2006-01-01")
    ap.add_argument("--uni-db", default=None)
    a = ap.parse_args()
    run(horizons=tuple(int(x) for x in a.horizons.split(",")), start=a.start, uni_db=a.uni_db)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
