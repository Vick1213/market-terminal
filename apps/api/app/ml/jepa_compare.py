"""Does JEPA actually HELP? — head-to-head under identical gates.

The only honest test of a representation is whether it ADDS information over what
you already have. This pits three feature sets against each other on the SAME
rows, SAME targets, SAME date-purged walk-forward + PBO/DSR:

  hand(56)    — the §12 stationary hand-feature matrix (the incumbent baseline).
  jepa(d)     — the post-trained TS-JEPA market-state embedding z_t alone.
  concat      — hand ⊕ jepa (the decisive test: does adding z beat hand alone?).

Read:
  * If concat ≈ hand, JEPA is redundant given the hand features — no help.
  * If concat > hand by more than noise, the temporal/regime structure JEPA
    compressed from a 64-day window carries information the per-row hand features
    miss. Held to the same gates so "help" must survive deflation, not just raise IC.
  * jepa-alone vs hand-alone is diagnostic (a low jepa-alone vol IC is expected —
    the windows.py per-NAME normalization strips cross-name vol levels — so the
    fair "help" verdict is concat-vs-hand, not jepa-alone).

Both feature sets are aligned on the shared (date, asset) index (inner join), so
every model sees identical samples. Targets/labels/gates are imported unchanged
from jepa_eval (which imports them from the §12 modules) — nothing here is a softer
bar than the baselines.

Run (after --posttrain):
  cd apps/api && .venv/bin/python -m app.ml.jepa --embed-posttrain   # embeddings_pt.npy
  .venv/bin/python -m app.ml.jepa_compare --horizons 5,21
"""
from __future__ import annotations

import argparse
import json
import warnings
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from app.ml.cross_section import demean_by_day
from app.ml.jepa_eval import _close_map, _targets, _walk_forward
from app.ml.windows import load_model_matrix
from app.ml.zoo import REGRESSORS

JEPA_DIR = Path(__file__).resolve().parents[4] / "data" / "ml" / "jepa"


def _load_emb(fname: str) -> pd.DataFrame:
    Z = np.load(JEPA_DIR / fname)
    dates = np.load(JEPA_DIR / "embed_dates.npy")
    aid = np.load(JEPA_DIR / "embed_asset_id.npy")
    man = json.loads((JEPA_DIR / "manifest.json").read_text())
    sym = np.array(man["assets"])[aid]
    idx = pd.MultiIndex.from_arrays([pd.to_datetime(dates, unit="D"), sym],
                                    names=["date", "asset"])
    df = pd.DataFrame(Z, index=idx, columns=[f"z{i}" for i in range(Z.shape[1])])
    return df.sort_index()


def _best(rows):
    """Pick the reported best-model row from a _walk_forward result list."""
    if not rows:
        return None
    for r in rows:
        if r["best"]:
            return r
    return max(rows, key=lambda r: r["ic"] if np.isfinite(r["ic"]) else -9)


def run(horizons=(5, 21), emb_file="embeddings_pt.npy", start="2006-01-01", uni_db=None):
    warnings.filterwarnings("ignore"); np.seterr(invalid="ignore")
    if not (JEPA_DIR / emb_file).exists():
        raise SystemExit(f"{emb_file} missing — run: python -m app.ml.jepa --embed-posttrain")
    repo = Path(__file__).resolve().parents[4]
    uni_db = Path(uni_db) if uni_db else repo / "data" / "ml" / "universe.duckdb"

    hand = load_model_matrix(start)                       # (date,asset) × 56
    hand.columns = [f"h{i}" for i in range(hand.shape[1])]
    emb = _load_emb(emb_file)                             # (date,asset) × d
    common = hand.index.intersection(emb.index)
    hand = hand.reindex(common); emb = emb.reindex(common)
    concat = pd.concat([hand, emb], axis=1)
    sets = {f"hand({hand.shape[1]})": hand, f"jepa({emb.shape[1]})": emb,
            f"concat({concat.shape[1]})": concat}
    print(f"=== JEPA HEAD-TO-HEAD ===  aligned rows={len(common):,}  "
          f"{common.get_level_values('date').min().date()}->"
          f"{common.get_level_values('date').max().date()}  "
          f"sets={list(sets)}", flush=True)

    uni = duckdb.connect(str(uni_db), read_only=True)
    syms = common.get_level_values("asset").unique().tolist()
    closes = _close_map(uni, syms)

    summary = []
    for h in horizons:
        for kind in ("fwd_vol", "rel_return"):
            tgt, raw = _targets(closes, common, h, kind)
            keep = tgt.notna().to_numpy()
            y0 = tgt.to_numpy(float)[keep]
            rawv = raw.to_numpy(float)[keep]
            d = common.get_level_values("date").to_numpy()[keep]
            y = demean_by_day(pd.Series(y0), d).to_numpy() if kind == "rel_return" else y0
            print(f"\n----- h={h}  [{kind}]  (n={keep.sum():,}) -----", flush=True)
            print(f"  {'feature set':14s} {'IC':>8} {'IC-t':>6} {'PBO':>5} {'Sharpe':>7} {'DSR':>6}")
            for sname, Xdf in sets.items():
                X = Xdf.to_numpy(float)[keep]
                rows = _walk_forward(X, y, rawv, d, kind, h, len(REGRESSORS) - 1)
                b = _best(rows)
                if not b:
                    print(f"  {sname:14s}  (no result)"); continue
                print(f"  {sname:14s} {_n(b['ic'],4):>8} {_n(b['ic_t'],1):>6} "
                      f"{_n(b['pbo'],2):>5} {_n(b['sharpe'],2):>7} {_n(b['dsr'],2):>6}"
                      f"  [{b['model']}]", flush=True)
                summary.append({"h": h, "kind": kind, "set": sname, **{
                    k: b[k] for k in ("ic", "ic_t", "pbo", "sharpe", "dsr", "model")}})
    uni.close()
    _verdict(pd.DataFrame(summary))
    return summary


def _verdict(df):
    print("\n================= DOES JEPA HELP? =================")
    for (h, kind), g in df.groupby(["h", "kind"]):
        g = g.set_index("set")
        hand = next((i for i in g.index if i.startswith("hand")), None)
        jep = next((i for i in g.index if i.startswith("jepa")), None)
        con = next((i for i in g.index if i.startswith("concat")), None)
        if not (hand and con):
            continue
        hic, cic, jic = g.loc[hand, "ic"], g.loc[con, "ic"], g.loc[jep, "ic"]
        delta = cic - hic
        tag = ("HELPS" if delta > 0.002 and g.loc[con, "ic_t"] > 2 else
               "no help" if abs(delta) <= 0.002 else "HURTS")
        print(f"  h={h:2d} [{kind:10s}]  hand IC {hic:+.4f} | jepa {jic:+.4f} | "
              f"concat {cic:+.4f}  Δ(concat-hand) {delta:+.4f}  → {tag}")
    print("  (HELPS requires concat IC > hand by >0.002 AND concat IC-t>2; gates still")
    print("   apply — a higher IC that fails PBO/DSR is not a real improvement.)")


def _n(x, d=3):
    return "—" if x is None or (isinstance(x, float) and not np.isfinite(x)) else f"{x:.{d}f}"


def _main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizons", default="5,21")
    ap.add_argument("--emb-file", default="embeddings_pt.npy")
    ap.add_argument("--start", default="2006-01-01")
    ap.add_argument("--uni-db", default=None)
    a = ap.parse_args()
    run(horizons=tuple(int(x) for x in a.horizons.split(",")),
        emb_file=a.emb_file, start=a.start, uni_db=a.uni_db)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
