"""Cross-sectional fundamental factor model (PLAN §12 track 2 — the only honest
shot at *return* alpha on this data).

§12 proved free price+macro data forecasts vol, not returns. The literature says
the one lever that produces real cross-sectional return IC is per-name
FUNDAMENTALS (value/quality/investment) — Gu-Kelly-Xiu, IPCA, Chen-Pelger — though
Avramov-Cheng-Metzker warn the edge largely attenuates for large-caps net of cost.
This builds those factors from the SEC EDGAR XBRL facts (data/ml/fundamentals.duckdb,
fetched with point-in-time `filed_date`s) and tests them under the SAME gated
walk-forward as everything else. We measure; we don't assume.

Leakage discipline: every fundamental is joined as-of its `filed_date` (the date it
became public via the filing) — NOT its period_end — so no number enters a feature
before the market could have seen it. Restatements are separate vintages; the
as-of join naturally picks the latest-knowable one.

Factors (balance-sheet only — clean instant values; earnings/flows skipped because
the fetched facts lack duration tags to separate quarterly vs annual NetIncome):
  bm    — book/market (StockholdersEquity / market cap)        [value]
  inv   — asset growth YoY, sign-flipped                        [investment]
  qual  — equity/assets (low leverage)                          [quality]
  size  — −log(market cap)                                      [size]
Composite = equal-weight z of the four. Higher = cheaper/safer/smaller.

Run:  cd apps/api && .venv/bin/python -m app.ml.factors            # eval verdict
"""
from __future__ import annotations

import argparse
import warnings
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from app.ml import labels
from app.ml.cross_section import DateWalkForward, cross_sectional_ic, demean_by_day
from app.ml.metrics import deflated_sharpe, pbo_cscv

REPO = Path(__file__).resolve().parents[4]
FUND_DB = REPO / "data" / "ml" / "fundamentals.duckdb"
UNI_DB = REPO / "data" / "ml" / "universe.duckdb"
SHARE_TAGS = ["CommonStockSharesOutstanding", "EntityCommonStockSharesOutstanding"]


def _facts() -> pd.DataFrame:
    con = duckdb.connect(str(FUND_DB), read_only=True)
    df = con.execute(
        "SELECT ticker, tag, period_end, value, filed_date FROM xbrl_facts "
        "WHERE value IS NOT NULL").df()
    con.close()
    df["filed_date"] = pd.to_datetime(df["filed_date"])
    df["period_end"] = pd.to_datetime(df["period_end"])
    return df


def _monthly_close(uni) -> pd.DataFrame:
    """Month-end close per ticker (long form: date, asset, close)."""
    df = uni.execute(
        "SELECT symbol, ts, close FROM ts_price WHERE source='yahoo' AND close>0").df()
    df["date"] = pd.to_datetime(df["ts"]).dt.normalize()
    df = df.sort_values("date")
    df["ym"] = df["date"].dt.to_period("M")
    me = df.groupby(["symbol", "ym"]).tail(1)  # last trading day of each month
    return me[["symbol", "date", "close"]].rename(columns={"symbol": "asset"})


def _asof(facts: pd.DataFrame, tag: str, grid: pd.DataFrame, offset_days: int = 0) -> pd.Series:
    """Point-in-time value of ``tag`` for each (asset, date) in ``grid`` — the
    latest fact with filed_date <= date(-offset). merge_asof per ticker."""
    sub = facts[facts.tag == tag][["ticker", "filed_date", "period_end", "value"]].dropna()
    # one value per (ticker, filed_date): the latest period_end filed that day
    sub = (sub.sort_values(["ticker", "filed_date", "period_end"])
              .groupby(["ticker", "filed_date"], as_index=False).last())
    out = []
    key = grid["date"] - pd.to_timedelta(offset_days, unit="D")
    g = grid.assign(_k=key)
    for asset, gg in g.groupby("asset"):
        s = sub[sub.ticker == asset][["filed_date", "value"]].sort_values("filed_date")
        if s.empty:
            continue
        m = pd.merge_asof(gg.sort_values("_k"), s.rename(columns={"filed_date": "_k"}),
                          on="_k", direction="backward")
        out.append(m[["date", "asset", "value"]])
    if not out:
        return pd.Series(dtype=float)
    r = pd.concat(out).set_index(["date", "asset"])["value"]
    return r


def build_factor_panel(start="2006-01-01") -> pd.DataFrame:
    facts = _facts()
    uni = duckdb.connect(str(UNI_DB), read_only=True)
    grid = _monthly_close(uni)
    uni.close()
    grid = grid[grid["date"] >= pd.Timestamp(start)].reset_index(drop=True)
    idx = grid.set_index(["date", "asset"])
    close = idx["close"]

    # PIT fundamentals as-of each month-end.
    equity = _asof(facts, "StockholdersEquity", grid)
    assets = _asof(facts, "Assets", grid)
    assets_yago = _asof(facts, "Assets", grid, offset_days=365)
    shares = _asof(facts, SHARE_TAGS[0], grid)
    shares2 = _asof(facts, SHARE_TAGS[1], grid)
    shares = shares.reindex(close.index)
    shares = shares.fillna(shares2.reindex(close.index))

    mcap = shares * close
    f = pd.DataFrame(index=close.index)
    f["bm"] = (equity.reindex(close.index) / mcap).replace([np.inf, -np.inf], np.nan)
    ag = (assets.reindex(close.index) / assets_yago.reindex(close.index) - 1.0)
    f["inv"] = -ag.replace([np.inf, -np.inf], np.nan)              # low growth = good
    f["qual"] = (equity.reindex(close.index) / assets.reindex(close.index))
    f["size"] = -np.log(mcap.replace(0, np.nan))                   # small = good
    f = f.dropna(how="all")
    # winsorize bm (a few negative-equity / tiny-mcap outliers)
    f["bm"] = f["bm"].clip(lower=0, upper=f["bm"].quantile(0.99))
    return f


def _zscore_by_day(s: pd.Series) -> pd.Series:
    d = s.index.get_level_values("date")
    g = s.groupby(d)
    return ((s - g.transform("mean")) / g.transform("std")).replace([np.inf, -np.inf], np.nan)


def composite(f: pd.DataFrame) -> pd.Series:
    z = pd.DataFrame({c: _zscore_by_day(f[c]) for c in ["bm", "inv", "qual", "size"]})
    return z.mean(axis=1, skipna=True)


def _fwd_return(grid_index: pd.MultiIndex) -> pd.Series:
    """~1-month forward demeaned return per (date, asset) from monthly closes."""
    uni = duckdb.connect(str(UNI_DB), read_only=True)
    out = {}
    for asset in grid_index.get_level_values("asset").unique():
        rows = uni.execute("SELECT ts, close FROM ts_price WHERE symbol=? AND source='yahoo' "
                           "AND close>0 ORDER BY ts", [asset]).fetchall()
        if not rows:
            continue
        s = pd.Series([r[1] for r in rows], index=pd.to_datetime([r[0] for r in rows]).normalize())
        out[asset] = labels.forward_log_return(s, 21)
    uni.close()
    fr = pd.concat(out, names=["asset", "date"]).swaplevel().sort_index()
    return fr.reindex(grid_index)


def evaluate(start="2006-01-01") -> None:
    warnings.filterwarnings("ignore"); np.seterr(invalid="ignore", divide="ignore")
    f = build_factor_panel(start)
    comp = composite(f)
    print(f"=== FUNDAMENTAL FACTOR MODEL ===  {f.shape[0]:,} (asset,month) rows, "
          f"{f.index.get_level_values('asset').nunique()} names, "
          f"{f.index.get_level_values('date').min().date()}→"
          f"{f.index.get_level_values('date').max().date()}\n")
    raw_fwd = _fwd_return(f.index)

    # cross-sectional IC of each factor + composite vs forward demeaned return
    d = f.index.get_level_values("date").to_numpy()
    fwd = raw_fwd.to_numpy(float)
    rel = demean_by_day(pd.Series(fwd, index=f.index), d).to_numpy()
    print(f"  {'factor':10s} {'IC':>8} {'IC-t':>7} {'months':>7} {'hit%':>5}")
    for col in ["bm", "inv", "qual", "size"]:
        r = cross_sectional_ic(d, f[col].to_numpy(float), rel)
        print(f"  {col:10s} {_n(r['ic']):>8} {_n(r['ic_t'],1):>7} {r['n_days']:>7} "
              f"{100*r['hit']:>4.0f}%")
    rc = cross_sectional_ic(d, comp.to_numpy(float), rel)
    print(f"  {'COMPOSITE':10s} {_n(rc['ic']):>8} {_n(rc['ic_t'],1):>7} {rc['n_days']:>7} "
          f"{100*rc['hit']:>4.0f}%")

    # gated walk-forward on the composite: L/S quintile portfolio, PBO, DSR
    keep = np.isfinite(comp.to_numpy(float)) & np.isfinite(rel)
    cv = comp.to_numpy(float)[keep]; yv = rel[keep]; dv = d[keep]
    rawv = fwd[keep]
    split = DateWalkForward(n_splits=6, min_train_days=24, embargo_days=1, horizon=1)
    fold_ic, pnl = [], []
    for tr, te in split.split(dv):
        if te.sum() < 50:
            continue
        r = cross_sectional_ic(dv[te], cv[te], yv[te])
        fold_ic.append(r["ic"])
        pnl.append(_ls(dv[te], cv[te], rawv[te]))
    if pnl:
        allpnl = np.concatenate(pnl)
        sh = float(allpnl.mean() / allpnl.std(ddof=1) * np.sqrt(12)) if allpnl.std() else np.nan
        S = np.array([[ic] for ic in fold_ic])
        pbo = pbo_cscv(np.column_stack([S, -S]))["pbo"] if len(fold_ic) >= 4 else float("nan")
        dsr = deflated_sharpe(allpnl, allpnl.mean()/allpnl.std(ddof=1), 5, 0.25)["dsr"] \
            if allpnl.std() else float("nan")
        print(f"\n  composite L/S (monthly, quintile): Sharpe {_n(sh,2)}  PBO {_n(pbo,2)}  "
              f"DSR {_n(dsr,2)}")
        gate = (rc["ic_t"] > 2 and sh > 0 and (pbo < 0.5) and dsr > 0.95)
        print(f"  VERDICT: {'EDGE clears gates — verify' if gate else 'no deflation-surviving cross-sectional return edge (expected: large-cap attenuation)'}")


def _ls(dates, score, raw, q=0.2):
    df = pd.DataFrame({"d": dates, "s": score, "r": raw}).dropna()
    pnl = []
    for _, g in df.groupby("d"):
        if len(g) < 10:
            continue
        k = max(1, int(len(g) * q))
        gg = g.sort_values("s")
        pnl.append(gg.tail(k)["r"].mean() - gg.head(k)["r"].mean())
    return np.array(pnl, float)


def latest_ranks(top: int = 6) -> dict:
    """Latest cross-sectional factor leaders/laggards — descriptive tilt for the
    strategist/panel (value+quality+small composite). Not a gated alpha claim."""
    f = build_factor_panel(start="2018-01-01")
    comp = composite(f)
    last_date = comp.index.get_level_values("date").max()
    cur = comp.xs(last_date, level="date").dropna().sort_values(ascending=False)
    fr = f.xs(last_date, level="date")
    def row(sym):
        r = fr.loc[sym]
        return {"symbol": sym, "score": round(float(comp.loc[(last_date, sym)]), 2),
                "bm": round(float(r["bm"]), 2) if np.isfinite(r["bm"]) else None,
                "qual": round(float(r["qual"]), 2) if np.isfinite(r["qual"]) else None}
    return {
        "as_of": last_date.date().isoformat(),
        "leaders": [row(s) for s in cur.head(top).index],
        "laggards": [row(s) for s in cur.tail(top).index][::-1],
    }


def _n(x, d=4):
    return "—" if x is None or (isinstance(x, float) and not np.isfinite(x)) else f"{x:.{d}f}"


def _main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2006-01-01")
    ap.add_argument("--ranks", action="store_true")
    a = ap.parse_args()
    if a.ranks:
        import json
        print(json.dumps(latest_ranks(), indent=2))
    else:
        evaluate(a.start)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
