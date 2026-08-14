"""Cross-sectional training + OOS report (PLAN §12 levers #1 + #2).

Ranks a wide single-name universe each day under leakage-safe, date-purged
walk-forward and reports the honest read on whether free price-derived factors
carry a cross-sectional edge — the setup that actually has enough independent
samples to clear the gates (unlike the per-index time-series path, which the
ALFRED-NFCI work showed was ~60% revision leakage and otherwise flat).

Two targets:
  rel_return — per-day cross-sectionally demeaned vol-scaled forward return
               (market-neutral: learn who out-performs). Full gates: cross-
               sectional IC-t, a quintile long/short Sharpe after cost, PBO, DSR.
  fwd_vol    — forward realised vol ranked across names (lever #2: vol is
               genuinely forecastable). IC + hit-rate only (no L/S return).

Prices come from the SEPARATE universe DB (read-only); macro/vintages from the
live market DB (read-only) via cross_section.RoutingDuck — the wide panel never
contends with the single-writer terminal DB.

Run:  cd apps/api && .venv/bin/python -m app.ml.xtrain
      .venv/bin/python -m app.ml.xtrain --horizons 5,21 --start 2006-01-01
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import warnings
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from app.ml import labels
from app.ml.cross_section import (
    DateWalkForward,
    RoutingDuck,
    cross_sectional_ic,
    demean_by_day,
)
from app.ml.features import build_model_matrix
from app.ml.metrics import deflated_sharpe, pbo_cscv
from app.ml.pipeline import build_pipeline
from app.ml.zoo import REGRESSORS

HORIZONS = (5, 21)
DEFAULT_START = "2006-01-01"  # macro coverage floor; price factors go deeper


def _close_map(uni_con, symbols) -> dict[str, pd.Series]:
    out = {}
    for s in symbols:
        rows = uni_con.execute(
            "SELECT ts, close FROM ts_price WHERE symbol=? AND source='yahoo' "
            "AND close IS NOT NULL ORDER BY ts", [s]).fetchall()
        if rows:
            out[s] = pd.Series([r[1] for r in rows],
                               index=pd.to_datetime([r[0] for r in rows]).normalize())
    return out


def _target_panel(closes: dict[str, pd.Series], matrix: pd.DataFrame, h: int,
                  kind: str) -> tuple[pd.Series, pd.Series]:
    """Build the (date,asset)-indexed target + raw forward return aligned to the
    matrix index. kind='rel_return' → vol-scaled fwd return; 'fwd_vol' → log fwd
    realised vol. Returns (target, raw_fwd_logret)."""
    tgt, raw = {}, {}
    for sym, close in closes.items():
        if kind == "fwd_vol":
            tgt[sym] = labels.forward_realized_vol(close, h)
        else:
            tgt[sym] = labels.vol_scaled_label(close, h)
        raw[sym] = labels.forward_log_return(close, h)
    tser = pd.concat({s: v for s, v in tgt.items()}, names=["asset", "date"]).swaplevel().sort_index()
    rser = pd.concat({s: v for s, v in raw.items()}, names=["asset", "date"]).swaplevel().sort_index()
    tser = tser.reindex(matrix.index)
    rser = rser.reindex(matrix.index)
    return tser, rser


def _ls_portfolio(dates, pred, raw_fwd, q=0.2) -> np.ndarray:
    """Per-day equal-weight long-top-q / short-bottom-q portfolio raw forward
    return. Daily-sampled h-day windows overlap → an optimistic Sharpe screen."""
    df = pd.DataFrame({"d": dates, "p": pred, "r": raw_fwd}).dropna()
    pnl = []
    for _, g in df.groupby("d"):
        if len(g) < 10:
            continue
        k = max(1, int(len(g) * q))
        gg = g.sort_values("p")
        short = gg.head(k)["r"].mean()
        long = gg.tail(k)["r"].mean()
        pnl.append(long - short)
    return np.array(pnl, dtype=float)


def _feature_version(columns) -> str:
    names = ",".join(sorted(columns))
    return hashlib.sha1(names.encode()).hexdigest()[:12]


def _git_sha(repo: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(repo),
            capture_output=True, text=True, check=True, timeout=5,
        )
        return out.stdout.strip()
    except Exception:
        return None


def _f(x):
    return None if x is None or (isinstance(x, float) and not np.isfinite(x)) else float(x)


def _ensure_xs_tables(con) -> None:
    """Parallel to train.py's ml_runs / ml_oos_metrics. A separate table pair
    (not a shared ``kind`` discriminator on ml_oos_metrics) because the
    cross-sectional harness has no single ``asset`` (it ranks a whole
    universe per day) and a different metric set — no ann_ret/auc/brier
    (no classification target here), no n_folds/min_train, and n_days
    (unique OOS test dates) is a different unit than n_oos (row count).
    Mixing rows into ml_oos_metrics would also silently leak into select.py's
    latest-run champion scan, which assumes one row per (asset, horizon,
    target, model) from the time-series harness.
    """
    con.execute(
        """CREATE TABLE IF NOT EXISTS xs_runs (
            run_id VARCHAR, ts TIMESTAMP, git_sha VARCHAR,
            feature_version VARCHAR, n_features INTEGER, n_names INTEGER,
            n_rows INTEGER, window_start VARCHAR, window_end VARCHAR,
            cost_bps DOUBLE, config VARCHAR);"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS xs_oos_metrics (
            run_id VARCHAR, horizon INTEGER, target VARCHAR, model VARCHAR,
            n_days INTEGER, ic DOUBLE, ic_t DOUBLE, hit_rate DOUBLE,
            sharpe DOUBLE, dsr DOUBLE, pbo DOUBLE, is_best BOOLEAN);"""
    )


def run(horizons=HORIZONS, start=DEFAULT_START, cost_bps=1.0, uni_db=None,
        persist=True):
    warnings.filterwarnings("ignore")
    np.seterr(invalid="ignore")
    repo = Path(__file__).resolve().parents[4]
    main_db = repo / "data" / "market.duckdb"
    uni_db = Path(uni_db) if uni_db else repo / "data" / "ml" / "universe.duckdb"
    if not uni_db.exists():
        raise SystemExit(f"universe DB missing: {uni_db} (run scratchpad/fetch_universe.py)")

    main = duckdb.connect(str(main_db), read_only=True)
    uni = duckdb.connect(str(uni_db), read_only=True)
    symbols = [r[0] for r in uni.execute(
        "SELECT DISTINCT symbol FROM ts_price WHERE source='yahoo'").fetchall()]
    duck = RoutingDuck(main, uni)
    print(f"=== cross-sectional train: {len(symbols)} names, start {start} ===", flush=True)
    print("CAVEAT: current-membership universe (mild survivorship bias); "
          "daily-overlapping h-day Sharpe is an optimistic screen.", flush=True)

    # Matrix build over 147 deep-history names is the slow part (single-thread
    # pandas joins). Cache it keyed by the universe DB mtime + start so repeat
    # experiments skip straight to modelling. Filtering to `start` AFTER build
    # preserves trailing-window warmup, so we cache the post-filter matrix per start.
    import pickle
    cache = uni_db.parent / f".xs_matrix_{int(uni_db.stat().st_mtime)}_{start}.pkl"
    if cache.exists():
        matrix = pickle.loads(cache.read_bytes())
        print(f"[cache] loaded matrix {matrix.shape} from {cache.name}", flush=True)
    else:
        print("[build] assembling PIT matrix over universe (one-time, ~min)...", flush=True)
        matrix, _breport = build_model_matrix(duck, assets=tuple(symbols))
        matrix = matrix[matrix.index.get_level_values("date") >= pd.Timestamp(start)]
        matrix = matrix.dropna(axis=1, how="all")
        for stale in uni_db.parent.glob(".xs_matrix_*.pkl"):
            stale.unlink()  # drop older caches
        cache.write_bytes(pickle.dumps(matrix))
        print(f"[build] cached matrix {matrix.shape} -> {cache.name}", flush=True)
    closes = _close_map(uni, symbols)
    print(f"matrix {matrix.shape}  "
          f"{matrix.index.get_level_values('asset').nunique()} names, "
          f"{matrix.index.get_level_values('date').min().date()}->"
          f"{matrix.index.get_level_values('date').max().date()}\n", flush=True)

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    fv = _feature_version(matrix.columns)
    git_sha = _git_sha(repo)
    window_start = str(matrix.index.get_level_values("date").min().date())
    window_end = str(matrix.index.get_level_values("date").max().date())

    rows = []
    for h in horizons:
        for kind in ("rel_return", "fwd_vol"):
            tgt, raw = _target_panel(closes, matrix, h, kind)
            keep = tgt.notna().to_numpy()
            X = matrix.to_numpy(float)[keep]
            dates = matrix.index.get_level_values("date").to_numpy()[keep]
            y = tgt.to_numpy(float)[keep]
            rawv = raw.to_numpy(float)[keep]
            if kind == "rel_return":
                y = demean_by_day(pd.Series(y), dates).to_numpy()
            splitter = DateWalkForward(n_splits=6, min_train_days=504,
                                       embargo_days=h, horizon=h)
            per_model_fold_ic = {}
            per_model_pooled = {}
            per_model_pnl = {}
            print(f"[h={h} {kind}] X={X.shape}  fitting models on {len(REGRESSORS)-1} "
                  f"× {splitter.n_splits} folds (n_jobs=-1)...", flush=True)
            for name, factory in REGRESSORS.items():
                if name == "naive_mean":
                    continue
                fold_ics, pooled_p, pooled_t, pooled_dates, pnl_all = [], [], [], [], []
                for fi, (tr, te) in enumerate(splitter.split(dates)):
                    if tr.sum() < 200 or te.sum() < 50:
                        continue
                    mtr = np.isfinite(y[tr])
                    est = factory()
                    try:
                        est.set_params(n_jobs=-1)  # use all 18 cores (RF/LGBM/ENetCV)
                    except (ValueError, AttributeError):
                        pass
                    pipe = build_pipeline(est)
                    pipe.fit(X[tr][mtr], y[tr][mtr])
                    pred = pipe.predict(X[te])
                    r = cross_sectional_ic(dates[te], pred, y[te])
                    fold_ics.append(r["ic"])
                    pooled_p.append(pred)
                    pooled_t.append(y[te])
                    pooled_dates.append(dates[te])
                    if kind == "rel_return":
                        pnl_all.append(_ls_portfolio(dates[te], pred, rawv[te]))
                if not pooled_p:
                    continue
                P = np.concatenate(pooled_p)
                T = np.concatenate(pooled_t)
                D = np.concatenate(pooled_dates)
                pooled = cross_sectional_ic(D, P, T)
                per_model_fold_ic[name] = fold_ics
                per_model_pooled[name] = pooled
                print(f"    {name:14s} IC={pooled['ic']:+.4f} t={pooled['ic_t']:.1f}", flush=True)
                if kind == "rel_return" and pnl_all:
                    per_model_pnl[name] = np.concatenate(pnl_all)

            if not per_model_pooled:
                continue
            # PBO across folds × models on per-fold IC.
            models = list(per_model_fold_ic.keys())
            nf = min(len(v) for v in per_model_fold_ic.values())
            S = np.array([[per_model_fold_ic[m][i] for m in models] for i in range(nf)])
            pbo = pbo_cscv(S)["pbo"] if nf >= 4 else float("nan")
            best = max(models, key=lambda m: per_model_pooled[m]["ic"]
                       if np.isfinite(per_model_pooled[m]["ic"]) else -9)

            for m in models:
                pm = per_model_pooled[m]
                sh = dsr = np.nan
                if kind == "rel_return" and m in per_model_pnl:
                    pnl = per_model_pnl[m]
                    if len(pnl) > 20 and pnl.std() > 0:
                        sh = float(pnl.mean() / pnl.std(ddof=1) * np.sqrt(252.0 / h))
                        if m == best:
                            srvar = float(np.nanvar([
                                (per_model_pnl[mm].mean()/per_model_pnl[mm].std(ddof=1))
                                for mm in per_model_pnl if per_model_pnl[mm].std() > 0])) or 1e-6
                            dsr = deflated_sharpe(pnl, pnl.mean()/pnl.std(ddof=1),
                                                  len(models)*len(horizons)*2, srvar)["dsr"]
                rows.append({"h": h, "target": kind, "model": m, "ic": pm["ic"],
                             "ic_t": pm["ic_t"], "n_days": pm["n_days"], "hit": pm["hit"],
                             "pbo": pbo, "sharpe": sh, "dsr": dsr, "best": m == best})
    if persist:
        runs_db = repo / "data" / "ml" / "runs.duckdb"
        runs_db.parent.mkdir(parents=True, exist_ok=True)
        con = duckdb.connect(str(runs_db))
        _ensure_xs_tables(con)
        con.execute(
            "INSERT INTO xs_runs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [run_id, datetime.now(timezone.utc), git_sha, fv, matrix.shape[1],
             len(symbols), matrix.shape[0], window_start, window_end, cost_bps,
             json.dumps({"horizons": list(horizons), "start": start,
                         "uni_db": str(uni_db)})],
        )
        for r in rows:
            con.execute(
                "INSERT INTO xs_oos_metrics VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [run_id, r["h"], r["target"], r["model"], int(r["n_days"]),
                 _f(r["ic"]), _f(r["ic_t"]), _f(r["hit"]), _f(r["sharpe"]),
                 _f(r["dsr"]), _f(r["pbo"]), bool(r["best"])],
            )
        con.close()

    _report(rows, len(symbols))
    main.close()
    uni.close()
    return rows


def _report(rows, n_names):
    df = pd.DataFrame(rows)
    print("============ CROSS-SECTIONAL OOS REPORT (date-purged walk-forward) ============")
    for (h, target), g in df.groupby(["h", "target"]):
        pbo = g["pbo"].iloc[0]
        print(f"\nh={h}  [{target}]   PBO={_n(pbo,2)}   (daily cross-sectional IC over {n_names} names)")
        print(f"  {'model':14s} {'IC':>8s} {'IC-t':>6s} {'days':>5s} {'hit%':>5s} {'Sharpe':>7s} {'DSR':>6s}")
        for _, r in g.iterrows():
            star = " *best" if r["best"] else ""
            print(f"  {r['model']:14s} {_n(r['ic'],4):>8} {_n(r['ic_t'],1):>6} "
                  f"{int(r['n_days']):>5} {_pct(r['hit']):>5} {_n(r['sharpe'],2):>7} {_n(r['dsr'],2):>6}{star}")
    _verdict(df)


def _verdict(df):
    real = df[df["best"]].copy()
    rr = real[real["target"] == "rel_return"]
    edge = rr[(rr["ic_t"].fillna(0) > 2) & (rr["sharpe"].fillna(-9) > 0)
              & (rr["pbo"].fillna(1) < 0.5) & (rr["dsr"].fillna(0) > 0.95)]
    print("\n--- VERDICT (rel_return gates: IC-t>2, L/S Sharpe>0 after cost, PBO<0.5, DSR>0.95) ---")
    if len(edge) == 0:
        print("  NO rel_return config clears the gates → no demonstrable cross-sectional")
        print("  return edge after deflation. (Check fwd_vol IC above — vol is the more")
        print("  forecastable target and may still be useful for sizing.)")
    else:
        for _, r in edge.iterrows():
            print(f"  EDGE: h={r['h']} {r['model']} IC-t={r['ic_t']:.1f} "
                  f"Sharpe={r['sharpe']:.2f} PBO={r['pbo']:.2f} DSR={r['dsr']:.2f}")
    vol = real[real["target"] == "fwd_vol"]
    if len(vol):
        v = vol.iloc[0]
        print(f"  [fwd_vol best] IC={_n(v['ic'],3)} IC-t={_n(v['ic_t'],1)} "
              f"hit={_pct(v['hit'])} → vol forecastability (expected strong).")


def _n(x, d=3):
    return "—" if x is None or (isinstance(x, float) and not np.isfinite(x)) else f"{x:.{d}f}"


def _pct(x):
    return "—" if x is None or not np.isfinite(x) else f"{x*100:.0f}%"


def _main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizons", default="5,21")
    ap.add_argument("--start", default=DEFAULT_START)
    ap.add_argument("--cost-bps", type=float, default=1.0)
    ap.add_argument("--uni-db", default=None, help="override universe DB path")
    ap.add_argument("--no-persist", dest="persist", action="store_false",
                     default=True, help="skip writing to data/ml/runs.duckdb")
    a = ap.parse_args()
    run(horizons=tuple(int(x) for x in a.horizons.split(",")), start=a.start,
        cost_bps=a.cost_bps, uni_db=a.uni_db, persist=a.persist)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
