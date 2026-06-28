"""Phase-2 training + OOS report (PLAN §12.4 / §12.5 / §12.8 step 2).

Trains the baseline zoo (ElasticNet / LightGBM / RandomForest + naive nulls;
Logistic / LightGBM for the direction target) across horizons × assets ×
targets under leakage-safe walk-forward, logs every OOS metric to a run table,
and prints the honest read on whether *any* edge exists.

This is the first place the §12.9 verdict gets tested empirically. **If the
baselines show no OOS edge after costs, that is the valuable result — stop
here** (don't build sequence nets to chase noise).

Reproducible: fixed model seeds, frozen feature spec (hashed into
``feature_version``), deterministic splits. The main market DB is opened
READ-ONLY (the live API holds the single writer); run metrics are written to a
separate ``data/ml/runs.duckdb`` so training never contends with ingestion.

Run:  cd apps/api && .venv/bin/python -m app.ml.train
      .venv/bin/python -m app.ml.train --horizons 5 --assets SPY
"""

from __future__ import annotations

import argparse
import hashlib
import json
import warnings
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from app.ml import labels
from app.ml.cv import WalkForward, spearman_ic
from app.ml.features import build_model_matrix
from app.ml.metrics import (
    classification_metrics,
    deflated_sharpe,
    ic_tstat,
    pbo_cscv,
    strategy_sharpe,
)
from app.ml.pipeline import collect_oos
from app.ml.zoo import CLASSIFIERS, REGRESSORS

HORIZONS = (1, 5, 21)
ASSETS = ("SPY", "QQQ")
COST_BPS = 1.0
# Default modelling floor: 2010 maximises feature coverage (VVIX 2006, VIX3M
# 2009, FRED 2005, cross-assets all present) while ~3x-ing the sample count vs
# the old 2021 price floor. Filtering AFTER feature engineering preserves the
# trailing-window warmup (features at the floor still use pre-floor history).
DEFAULT_START = "2010-01-01"


class _RO:
    """Minimal read-only DuckDB shim exposing the fetchall the builder needs."""

    def __init__(self, path: str) -> None:
        self.c = duckdb.connect(path, read_only=True)

    def fetchall(self, sql, params=None):
        return self.c.execute(sql, params or []).fetchall()


def _feature_version(columns) -> str:
    names = ",".join(sorted(columns))
    return hashlib.sha1(names.encode()).hexdigest()[:12]


def _close_series(duck, asset: str) -> pd.Series:
    rows = duck.fetchall(
        "SELECT ts, close FROM ts_price WHERE symbol=? AND source='yahoo' "
        "AND close IS NOT NULL ORDER BY ts",
        [asset],
    )
    idx = pd.to_datetime([r[0] for r in rows]).normalize()
    return pd.Series([r[1] for r in rows], index=idx, name="close")


def _per_fold_ic(oos) -> dict[int, float]:
    out: dict[int, float] = {}
    for fid in sorted(set(oos.fold_ids.tolist())):
        m = oos.fold_ids == fid
        out[fid] = spearman_ic(oos.pred[m], oos.true[m])
    return out


def _align(matrix_asset: pd.DataFrame, close: pd.Series, h: int):
    """Build X, y_reg, y_up, raw_fwd, label_times all aligned on a common index
    of decision dates that have a resolved h-day label.
    """
    y_reg = labels.vol_scaled_label(close, h)
    raw = labels.forward_log_return(close, h)
    lt = labels.label_times(close.index, h)
    tb = labels.triple_barrier_labels(close, h)
    y_up = (tb["label"] > 0).astype(int)

    idx = matrix_asset.index
    idx = idx.intersection(lt.index).intersection(y_reg.dropna().index)
    idx = idx.intersection(y_up.index)
    idx = idx.sort_values()

    X = matrix_asset.loc[idx]
    label_times = pd.DataFrame(
        {"t_decision": idx, "t_end": lt.reindex(idx).values}, index=idx
    )
    return (
        X.to_numpy(dtype=float),
        y_reg.reindex(idx).to_numpy(dtype=float),
        y_up.reindex(idx).to_numpy(dtype=float),
        raw.reindex(idx).to_numpy(dtype=float),
        label_times,
        idx,
    )


def _ensure_tables(con) -> None:
    con.execute(
        """CREATE TABLE IF NOT EXISTS ml_runs (
            run_id VARCHAR, ts TIMESTAMP, feature_version VARCHAR,
            n_features INTEGER, window_start VARCHAR, window_end VARCHAR,
            cost_bps DOUBLE, config VARCHAR);"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS ml_oos_metrics (
            run_id VARCHAR, asset VARCHAR, horizon INTEGER, target VARCHAR,
            model VARCHAR, n_oos INTEGER, n_folds INTEGER, min_train INTEGER,
            oos_ic DOUBLE, ic_mean_fold DOUBLE, ic_tstat DOUBLE,
            sharpe DOUBLE, hit_rate DOUBLE, ann_ret DOUBLE,
            auc DOUBLE, brier DOUBLE,
            group_pbo DOUBLE, dsr DOUBLE, is_best BOOLEAN);"""
    )


def run(horizons=HORIZONS, assets=ASSETS, cost_bps: float = COST_BPS,
        start: str = DEFAULT_START) -> dict:
    # Library deprecation chatter / harmless empty-slice means would drown the
    # report; the harness logic is exercised by test_leakage, not these.
    warnings.filterwarnings("ignore")
    np.seterr(invalid="ignore")
    root = Path(__file__).resolve().parents[4]  # repo root (apps/api/app/ml/train.py)
    main_db = root / "data" / "market.duckdb"
    out_dir = root / "data" / "ml"
    out_dir.mkdir(parents=True, exist_ok=True)
    runs_db = out_dir / "runs.duckdb"

    duck = _RO(str(main_db))
    matrix, report = build_model_matrix(duck, assets=tuple(assets))
    if start:
        # Floor AFTER engineering so trailing-window features keep their warmup.
        keep = matrix.index.get_level_values("date") >= pd.Timestamp(start)
        matrix = matrix[keep]
        report.start = start
    fv = _feature_version(matrix.columns)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    print(f"=== ML phase-2 train  run={run_id}  feature_version={fv} ===")
    print(f"feature matrix: {matrix.shape}  window {report.start}->{report.end}")
    if report.skipped:
        print(f"skipped specs: {report.skipped}")
    print(f"caveats: {report.caveats[0]}\n")

    rows: list[dict] = []
    for asset in assets:
        if asset not in matrix.index.get_level_values("asset"):
            print(f"  [skip {asset}] no rows in matrix")
            continue
        Xasset = matrix.xs(asset, level="asset").sort_index()
        close = _close_series(duck, asset)
        for h in horizons:
            X, y_reg, y_up, raw, lt, idx = _align(Xasset, close, h)
            splitter = WalkForward(n_splits=6, min_train=252, embargo_pct=0.02)

            # --- regression target (vol-scaled forward return) --------------
            reg_oos = {}
            for name, factory in REGRESSORS.items():
                reg_oos[name] = collect_oos(
                    factory, X, y_reg, lt, splitter, task="regression"
                )
            rows += _score_group(
                run_id, asset, h, "reg_volscaled", reg_oos, raw, h, cost_bps,
                signal_of=lambda o: o.pred, n_trials=len(REGRESSORS),
            )

            # --- classification target (triple-barrier direction) ----------
            clf_oos = {}
            for name, factory in CLASSIFIERS.items():
                clf_oos[name] = collect_oos(
                    factory, X, y_reg, lt, splitter,
                    task="classification", y_class=y_up,
                )
            rows += _score_group(
                run_id, asset, h, "clf_direction", clf_oos, raw, h, cost_bps,
                signal_of=lambda o: o.pred, n_trials=len(CLASSIFIERS),
                proba=True,
            )

    # --- persist + report ---------------------------------------------------
    con = duckdb.connect(str(runs_db))
    _ensure_tables(con)
    con.execute(
        "INSERT INTO ml_runs VALUES (?,?,?,?,?,?,?,?)",
        [run_id, datetime.now(timezone.utc), fv, matrix.shape[1],
         report.start, report.end, cost_bps,
         json.dumps({"horizons": list(horizons), "assets": list(assets)})],
    )
    for r in rows:
        con.execute(
            "INSERT INTO ml_oos_metrics VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [r[k] for k in (
                "run_id", "asset", "horizon", "target", "model", "n_oos",
                "n_folds", "min_train", "oos_ic", "ic_mean_fold", "ic_tstat",
                "sharpe", "hit_rate", "ann_ret", "auc", "brier",
                "group_pbo", "dsr", "is_best")],
        )
    con.close()

    _print_report(rows)
    return {"run_id": run_id, "runs_db": str(runs_db), "rows": rows,
            "report": asdict(report)}


def _score_group(
    run_id, asset, h, target, oos_by_model, raw, horizon, cost_bps,
    *, signal_of, n_trials, proba=False,
) -> list[dict]:
    """Compute every metric for one (asset, horizon, target) model comparison,
    plus the group-level PBO and the best model's Deflated Sharpe.
    """
    models = list(oos_by_model.keys())
    # Per-fold IC matrix (folds × models) for PBO.
    fold_ic = {m: _per_fold_ic(o) for m, o in oos_by_model.items()}
    all_folds = sorted({f for d in fold_ic.values() for f in d})
    S = np.full((len(all_folds), len(models)), np.nan)
    for j, m in enumerate(models):
        for i, f in enumerate(all_folds):
            S[i, j] = fold_ic[m].get(f, np.nan)
    pbo = pbo_cscv(S)["pbo"]

    per_model = {}
    sharpes_perperiod = {}
    for m, o in oos_by_model.items():
        ics = list(fold_ic[m].values())
        ic_mean, ic_t = ic_tstat(ics)
        # Sharpe of sign(signal) long/short on raw forward returns. raw is
        # indexed by the same aligned idx; o.true rows correspond 1:1 with the
        # OOS test positions, so pull raw via the pooled fold_index order.
        oos_pos = np.concatenate(o.fold_index) if o.fold_index else np.array([], int)
        sig = signal_of(o)
        sh = strategy_sharpe(sig, raw[oos_pos], horizon, cost_bps=cost_bps)
        # Per-period (non-annualised) pnl for DSR.
        pos = np.sign(sig)
        r = raw[oos_pos]
        msk = np.isfinite(pos) & np.isfinite(r)
        pnl = (pos * r)[msk]
        sharpes_perperiod[m] = (
            float(pnl.mean() / pnl.std(ddof=1)) if len(pnl) > 2 and pnl.std() > 0 else np.nan
        )
        clf = classification_metrics(o.proba, o.true > 0) if proba else {"auc": np.nan, "brier": np.nan}
        per_model[m] = {
            "oos_ic": spearman_ic(o.pred, o.true),
            "ic_mean_fold": ic_mean, "ic_tstat": ic_t,
            "sharpe": sh["sharpe"], "hit_rate": sh.get("hit_rate", np.nan),
            "ann_ret": sh.get("ann_ret", np.nan),
            "auc": clf["auc"], "brier": clf["brier"],
            "n_oos": len(o.true), "n_folds": o.n_folds, "min_train": o.min_train,
            "pnl": pnl,
        }

    # Best model = highest OOS IC among the non-naive models.
    ranked = [m for m in models if not m.startswith("naive")]
    best = max(ranked, key=lambda m: (per_model[m]["oos_ic"] if np.isfinite(per_model[m]["oos_ic"]) else -9))
    sr_var = float(np.nanvar([v for v in sharpes_perperiod.values() if np.isfinite(v)])) or 1e-6
    dsr = deflated_sharpe(per_model[best]["pnl"], sharpes_perperiod[best], n_trials, sr_var)["dsr"]

    out = []
    for m in models:
        pm = per_model[m]
        out.append({
            "run_id": run_id, "asset": asset, "horizon": h, "target": target,
            "model": m, "n_oos": pm["n_oos"], "n_folds": pm["n_folds"],
            "min_train": pm["min_train"], "oos_ic": _f(pm["oos_ic"]),
            "ic_mean_fold": _f(pm["ic_mean_fold"]), "ic_tstat": _f(pm["ic_tstat"]),
            "sharpe": _f(pm["sharpe"]), "hit_rate": _f(pm["hit_rate"]),
            "ann_ret": _f(pm["ann_ret"]), "auc": _f(pm["auc"]), "brier": _f(pm["brier"]),
            "group_pbo": _f(pbo), "dsr": _f(dsr if m == best else np.nan),
            "is_best": (m == best),
        })
    return out


def _f(x):
    return None if x is None or (isinstance(x, float) and not np.isfinite(x)) else float(x)


def _print_report(rows: list[dict]) -> None:
    df = pd.DataFrame(rows)
    print("\n===================  OOS REPORT (walk-forward, after "
          f"{COST_BPS}bps cost)  ===================")
    for (asset, h, target), g in df.groupby(["asset", "horizon", "target"]):
        pbo = g["group_pbo"].iloc[0]
        print(f"\n{asset}  h={h}  [{target}]   group PBO={_p(pbo)}")
        print(f"  {'model':14s} {'OOS-IC':>8s} {'IC-t':>6s} {'Sharpe':>7s} "
              f"{'hit%':>6s} {'AUC':>6s} {'Brier':>6s} {'DSR':>6s}")
        for _, r in g.iterrows():
            star = " *best" if r["is_best"] else ""
            print(f"  {r['model']:14s} {_n(r['oos_ic']):>8} {_n(r['ic_tstat'],1):>6} "
                  f"{_n(r['sharpe'],2):>7} {_pct(r['hit_rate']):>6} "
                  f"{_n(r['auc'],3):>6} {_n(r['brier'],3):>6} {_n(r['dsr'],2):>6}{star}")
    _verdict(df)


def _verdict(df: pd.DataFrame) -> None:
    real = df[(~df["model"].str.startswith("naive")) & df["is_best"]].copy()
    edge = real[
        (real["ic_tstat"].fillna(0) > 2.0)
        & (real["sharpe"].fillna(-9) > 0)
        & (real["group_pbo"].fillna(1) < 0.5)
        & (real["dsr"].fillna(0) > 0.95)
    ]
    print("\n--- VERDICT (gates: IC t>2, Sharpe>0 after cost, PBO<0.5, DSR>0.95) ---")
    if len(edge) == 0:
        print("  NO configuration clears the gates → no demonstrable OOS edge.")
        print("  Per §12.9 this is a VALID, money-saving result: do NOT proceed to")
        print("  sequence nets (phase 3) to chase noise. The harness did its job.")
    else:
        for _, r in edge.iterrows():
            print(f"  EDGE: {r['asset']} h={r['horizon']} {r['target']} "
                  f"{r['model']}  IC-t={r['ic_tstat']:.1f} Sharpe={r['sharpe']:.2f} "
                  f"PBO={r['group_pbo']:.2f} DSR={r['dsr']:.2f}")


def _n(x, d=3):
    return "—" if x is None or (isinstance(x, float) and not np.isfinite(x)) else f"{x:.{d}f}"


def _pct(x):
    return "—" if x is None or not np.isfinite(x) else f"{x*100:.0f}%"


def _p(x):
    return "—" if x is None or not np.isfinite(x) else f"{x:.2f}"


def _main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizons", default="1,5,21")
    ap.add_argument("--assets", default="SPY,QQQ")
    ap.add_argument("--cost-bps", type=float, default=COST_BPS)
    ap.add_argument("--start", default=DEFAULT_START, help="modelling floor date")
    a = ap.parse_args()
    run(
        horizons=tuple(int(x) for x in a.horizons.split(",")),
        assets=tuple(a.assets.split(",")),
        cost_bps=a.cost_bps,
        start=a.start,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
