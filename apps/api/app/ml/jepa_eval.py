"""Evaluate the JEPA market-state embeddings — held to the SAME bar as §12.

Three probes on ``embeddings.npy`` (the frozen TS-JEPA encoder's market-state
vector z_t for every legal window), each reusing the existing leak-safe machinery
so nothing here is a softer test than the hand-feature baselines:

  1. forward vol  — does z_t rank forward realised vol across names? (lever #2;
     expected STRONG per §12). Cross-sectional IC + hit-rate.
  2. returns      — does a model on z_t rank forward demeaned (market-neutral)
     returns? FULL gates: cross-sectional IC-t, quintile L/S Sharpe after cost,
     PBO, DSR — plus a strict forward holdout (train≤2018 / test≥2021). This is
     the falsification the user asked for, on the JEPA representation specifically.
     Per §12 the honest prior is NO_EDGE; we measure, we don't assume.
  3. regime       — KMeans over the daily market-mean embedding → discrete states;
     report per-state forward vol/return, persistence, and the transition matrix.
     Descriptive (no tradable claim), for the dashboard regime read.

Leakage discipline is inherited: embeddings come from windows whose target block
is strictly future and whose features are the leak-gated causal matrix; the probes
add date-purged walk-forward (purge+embargo) on top. The encoder was trained
self-supervised over ALL dates — that is acceptable for a representation (no label
touched), but the strict forward holdout additionally guards against the encoder
having merely memorised a regime that happens to recur.

Run (after pretraining):
  cd apps/api && .venv/bin/python -m app.ml.jepa --embed      # writes embeddings
  .venv/bin/python -m app.ml.jepa_eval --horizons 5,21
"""
from __future__ import annotations

import argparse
import json
import warnings
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from app.ml import labels
from app.ml.cross_section import DateWalkForward, cross_sectional_ic, demean_by_day
from app.ml.metrics import deflated_sharpe, pbo_cscv
from app.ml.pipeline import build_pipeline
from app.ml.xtrain import _ls_portfolio
from app.ml.zoo import REGRESSORS

JEPA_DIR = Path(__file__).resolve().parents[4] / "data" / "ml" / "jepa"
HOLDOUT_TRAIN_END = "2018-12-31"
HOLDOUT_TEST_START = "2021-01-01"  # COVID excluded both sides (matches §12 falsify)


def _load_embeddings():
    Z = np.load(JEPA_DIR / "embeddings.npy")
    dates = np.load(JEPA_DIR / "embed_dates.npy")          # days since epoch
    aid = np.load(JEPA_DIR / "embed_asset_id.npy")
    man = json.loads((JEPA_DIR / "manifest.json").read_text())
    assets = np.array(man["assets"])
    sym = assets[aid]
    ts = pd.to_datetime(dates, unit="D")
    idx = pd.MultiIndex.from_arrays([ts, sym], names=["date", "asset"])
    emb = pd.DataFrame(Z, index=idx).sort_index()
    return emb, man


def _close_map(uni_con, symbols):
    out = {}
    for s in symbols:
        rows = uni_con.execute(
            "SELECT ts, close FROM ts_price WHERE symbol=? AND source='yahoo' "
            "AND close IS NOT NULL ORDER BY ts", [s]).fetchall()
        if rows:
            out[s] = pd.Series([r[1] for r in rows],
                               index=pd.to_datetime([r[0] for r in rows]).normalize())
    return out


def _targets(closes, index, h, kind):
    tgt, raw = {}, {}
    for sym, close in closes.items():
        tgt[sym] = (labels.forward_realized_vol(close, h) if kind == "fwd_vol"
                    else labels.vol_scaled_label(close, h))
        raw[sym] = labels.forward_log_return(close, h)
    tser = pd.concat(tgt, names=["asset", "date"]).swaplevel().sort_index().reindex(index)
    rser = pd.concat(raw, names=["asset", "date"]).swaplevel().sort_index().reindex(index)
    return tser, rser


def _walk_forward(X, y, raw, dates, kind, h, n_models):
    """Date-purged walk-forward over the embeddings. Returns per-model pooled IC
    + PBO + (for returns) L/S Sharpe & DSR. Mirrors xtrain exactly."""
    splitter = DateWalkForward(n_splits=6, min_train_days=504, embargo_days=h, horizon=h)
    fold_ic, pooled, pnl = {}, {}, {}
    for name, factory in REGRESSORS.items():
        if name == "naive_mean":
            continue
        ics, P, T, D, pn = [], [], [], [], []
        for tr, te in splitter.split(dates):
            if tr.sum() < 200 or te.sum() < 50:
                continue
            mtr = np.isfinite(y[tr])
            est = factory()
            try:
                est.set_params(n_jobs=-1)
            except (ValueError, AttributeError):
                pass
            pipe = build_pipeline(est)
            pipe.fit(X[tr][mtr], y[tr][mtr])
            pred = pipe.predict(X[te])
            ics.append(cross_sectional_ic(dates[te], pred, y[te])["ic"])
            P.append(pred); T.append(y[te]); D.append(dates[te])
            if kind == "rel_return":
                pn.append(_ls_portfolio(dates[te], pred, raw[te]))
        if not P:
            continue
        fold_ic[name] = ics
        pooled[name] = cross_sectional_ic(np.concatenate(D), np.concatenate(P), np.concatenate(T))
        if kind == "rel_return" and pn:
            pnl[name] = np.concatenate(pn)
    if not pooled:
        return None
    models = list(fold_ic.keys())
    nf = min(len(v) for v in fold_ic.values())
    S = np.array([[fold_ic[m][i] for m in models] for i in range(nf)])
    pbo = pbo_cscv(S)["pbo"] if nf >= 4 else float("nan")
    best = max(models, key=lambda m: pooled[m]["ic"] if np.isfinite(pooled[m]["ic"]) else -9)
    rows = []
    for m in models:
        sh = dsr = np.nan
        if kind == "rel_return" and m in pnl and len(pnl[m]) > 20 and pnl[m].std() > 0:
            p = pnl[m]
            sh = float(p.mean() / p.std(ddof=1) * np.sqrt(252.0 / h))
            if m == best:
                srvar = float(np.nanvar([pnl[x].mean()/pnl[x].std(ddof=1)
                              for x in pnl if pnl[x].std() > 0])) or 1e-6
                dsr = deflated_sharpe(p, p.mean()/p.std(ddof=1), n_models * 2, srvar)["dsr"]
        rows.append({"model": m, "ic": pooled[m]["ic"], "ic_t": pooled[m]["ic_t"],
                     "n_days": pooled[m]["n_days"], "hit": pooled[m]["hit"],
                     "pbo": pbo, "sharpe": sh, "dsr": dsr, "best": m == best})
    return rows


def _strict_holdout(X, y, dates, kind, h):
    """One forward split: fit on ≤2018, test on ≥2021. lightgbm + elasticnet."""
    from app.ml.zoo import REGRESSORS as R
    tr = dates < np.datetime64(HOLDOUT_TRAIN_END)
    te = dates >= np.datetime64(HOLDOUT_TEST_START)
    out = {}
    for name in ("elasticnet", "lightgbm"):
        if name not in R:
            continue
        mtr = tr & np.isfinite(y)
        if mtr.sum() < 200 or te.sum() < 50:
            continue
        pipe = build_pipeline(R[name]())
        pipe.fit(X[mtr], y[mtr])
        pred = pipe.predict(X[te])
        out[name] = cross_sectional_ic(dates[te], pred, y[te])
    return out


def _regime_probe(emb, closes, k=4, h=21):
    """KMeans over the daily market-mean embedding → states; per-state forward
    21d realised vol & return, persistence, transition matrix."""
    from sklearn.cluster import KMeans
    from sklearn.preprocessing import StandardScaler

    daily = emb.groupby(level="date").mean()              # market-mean state per day
    daily = daily.dropna()
    Zs = StandardScaler().fit_transform(daily.to_numpy())
    km = KMeans(n_clusters=k, n_init=10, random_state=0).fit(Zs)
    lab = pd.Series(km.labels_, index=daily.index, name="regime")

    # SPY-proxy forward stats from the equal-weight universe mean close return.
    rets = pd.DataFrame({s: np.log(c).diff() for s, c in closes.items()})
    mkt = rets.mean(axis=1)                                # equal-weight market
    fwd_ret = mkt.shift(-1).rolling(h).sum().shift(-(h - 1))
    fwd_vol = mkt.shift(-1).rolling(h).std().shift(-(h - 1)) * np.sqrt(252)
    df = pd.DataFrame({"regime": lab}).join(
        pd.DataFrame({"fwd_ret": fwd_ret, "fwd_vol": fwd_vol})).dropna()
    print("\n=== REGIME PROBE (KMeans k=%d over daily market-mean state) ===" % k)
    print(f"  {'state':>5} {'days':>6} {'fwd21_ret%':>11} {'fwd21_vol%':>11}")
    for r, g in df.groupby("regime"):
        print(f"  {int(r):>5} {len(g):>6} {100*g['fwd_ret'].mean():>11.2f} "
              f"{100*g['fwd_vol'].mean():>11.1f}")
    # persistence + transitions
    ls = lab.to_numpy()
    trans = np.zeros((k, k))
    for a, b in zip(ls[:-1], ls[1:]):
        trans[a, b] += 1
    trans = trans / trans.sum(1, keepdims=True).clip(min=1)
    persist = float(np.mean([trans[i, i] for i in range(k)]))
    print(f"  mean 1-day self-persistence: {persist:.2f}  "
          f"(higher = stickier, more regime-like)")


def run(horizons=(5, 21), uni_db=None):
    warnings.filterwarnings("ignore"); np.seterr(invalid="ignore")
    if not (JEPA_DIR / "embeddings.npy").exists():
        raise SystemExit("embeddings.npy missing — run: python -m app.ml.jepa --embed")
    repo = Path(__file__).resolve().parents[4]
    uni_db = Path(uni_db) if uni_db else repo / "data" / "ml" / "universe.duckdb"
    emb, man = _load_embeddings()
    uni = duckdb.connect(str(uni_db), read_only=True)
    syms = emb.index.get_level_values("asset").unique().tolist()
    closes = _close_map(uni, syms)
    print(f"=== JEPA EMBEDDING EVAL ===  z dim={emb.shape[1]}  rows={len(emb):,}  "
          f"{len(syms)} names  {emb.index.get_level_values('date').min().date()}->"
          f"{emb.index.get_level_values('date').max().date()}")

    allrows = []
    for h in horizons:
        for kind in ("fwd_vol", "rel_return"):
            tgt, raw = _targets(closes, emb.index, h, kind)
            keep = tgt.notna().to_numpy()
            X = emb.to_numpy(float)[keep]
            y = tgt.to_numpy(float)[keep]
            rawv = raw.to_numpy(float)[keep]
            d = emb.index.get_level_values("date").to_numpy()[keep]
            if kind == "rel_return":
                y = demean_by_day(pd.Series(y), d).to_numpy()
            rows = _walk_forward(X, y, rawv, d, kind, h, len(REGRESSORS) - 1)
            if not rows:
                continue
            pbo = rows[0]["pbo"]
            print(f"\nh={h} [{kind}]  PBO={_n(pbo,2)}")
            print(f"  {'model':14s} {'IC':>8} {'IC-t':>6} {'days':>5} {'hit%':>5} {'Sharpe':>7} {'DSR':>6}")
            for r in rows:
                print(f"  {r['model']:14s} {_n(r['ic'],4):>8} {_n(r['ic_t'],1):>6} "
                      f"{int(r['n_days']):>5} {_pct(r['hit']):>5} {_n(r['sharpe'],2):>7} "
                      f"{_n(r['dsr'],2):>6}{' *best' if r['best'] else ''}")
                r.update(h=h, target=kind)
                allrows.append(r)
            ho = _strict_holdout(X, y, d, kind, h)
            if ho:
                print("  strict holdout (≤2018→≥2021):  " +
                      "  ".join(f"{m} IC={v['ic']:+.3f}(t{v['ic_t']:.1f})" for m, v in ho.items()))

    _verdict(pd.DataFrame(allrows))
    _regime_probe(emb, closes)
    uni.close()
    return allrows


def _verdict(df):
    if df.empty:
        print("\nno rows"); return
    rr = df[(df.target == "rel_return") & df.best]
    edge = rr[(rr.ic_t.fillna(0) > 2) & (rr.sharpe.fillna(-9) > 0)
              & (rr.pbo.fillna(1) < 0.5) & (rr.dsr.fillna(0) > 0.95)]
    print("\n--- VERDICT (return gates: IC-t>2, L/S Sharpe>0 after cost, PBO<0.5, DSR>0.95) ---")
    if len(edge) == 0:
        print("  NO return config clears the gates on the JEPA embeddings → consistent")
        print("  with §12: the representation does NOT manufacture deflation-surviving")
        print("  return alpha. (See fwd_vol IC — that is where the signal lives.)")
    else:
        for _, r in edge.iterrows():
            print(f"  EDGE: h={r.h} {r.model} IC-t={r.ic_t:.1f} Sharpe={r.sharpe:.2f} "
                  f"PBO={r.pbo:.2f} DSR={r.dsr:.2f}  ← verify before believing")
    vol = df[(df.target == "fwd_vol") & df.best]
    for _, v in vol.iterrows():
        print(f"  [fwd_vol h={v.h} best] IC={_n(v.ic,3)} IC-t={_n(v.ic_t,1)} hit={_pct(v.hit)}")


def _n(x, d=3):
    return "—" if x is None or (isinstance(x, float) and not np.isfinite(x)) else f"{x:.{d}f}"


def _pct(x):
    return "—" if x is None or not np.isfinite(x) else f"{x*100:.0f}%"


def _main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizons", default="5,21")
    ap.add_argument("--uni-db", default=None)
    a = ap.parse_args()
    run(horizons=tuple(int(x) for x in a.horizons.split(",")), uni_db=a.uni_db)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
