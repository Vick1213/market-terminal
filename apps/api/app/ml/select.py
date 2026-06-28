"""Champion selection + gating (PLAN §12.5 / §12.8 step 2 → registry).

Reads the latest run's OOS metrics from ``data/ml/runs.duckdb`` and applies the
§12.5 belief gates BEFORE any model is allowed to call itself a champion:

    IC t-stat > 2      — the signal is stable across folds, not one lucky split
    Sharpe > 0         — it survives turnover cost
    PBO < 0.5          — the in-sample winner isn't an OOS coin-flip
    DSR > 0.95         — the Sharpe survives the multiple-testing deflation

A model that clears all four per (asset, horizon, target) is written to a
versioned ``data/ml/models/champions.json`` registry. If nothing clears them,
the registry records a ``NO_EDGE`` verdict — per §12.9 that is the intended,
money-saving outcome and the explicit signal to NOT build phase 3.

This module only *selects and records*; it never lowers a gate to manufacture a
champion. Run after train.py:
    cd apps/api && .venv/bin/python -m app.ml.select
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import duckdb

GATES = {"ic_tstat": 2.0, "sharpe": 0.0, "pbo": 0.5, "dsr": 0.95}


def _latest_run(con) -> str | None:
    row = con.execute("SELECT run_id FROM ml_runs ORDER BY ts DESC LIMIT 1").fetchone()
    return row[0] if row else None


def select(runs_db: str | None = None) -> dict:
    root = Path(__file__).resolve().parents[4]
    runs_db = runs_db or str(root / "data" / "ml" / "runs.duckdb")
    models_dir = root / "data" / "ml" / "models"
    models_dir.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect(runs_db, read_only=True)
    run_id = _latest_run(con)
    if run_id is None:
        con.close()
        raise SystemExit("no runs found — run app.ml.train first")

    cols = [
        "asset", "horizon", "target", "model", "oos_ic", "ic_tstat",
        "sharpe", "hit_rate", "auc", "brier", "group_pbo", "dsr", "is_best",
    ]
    rows = con.execute(
        f"SELECT {','.join(cols)} FROM ml_oos_metrics WHERE run_id=? AND is_best",
        [run_id],
    ).fetchall()
    con.close()

    champions, rejected = [], []
    for r in rows:
        d = dict(zip(cols, r))
        passed = (
            (d["ic_tstat"] or -9) > GATES["ic_tstat"]
            and (d["sharpe"] or -9) > GATES["sharpe"]
            and (d["group_pbo"] if d["group_pbo"] is not None else 1) < GATES["pbo"]
            and (d["dsr"] or 0) > GATES["dsr"]
        )
        (champions if passed else rejected).append(d)

    verdict = "EDGE_FOUND" if champions else "NO_EDGE"
    registry = {
        "run_id": run_id,
        "selected_at": datetime.now(timezone.utc).isoformat(),
        "gates": GATES,
        "verdict": verdict,
        "champions": champions,
        "best_per_group_rejected": rejected,
        "note": (
            "Per PLAN §12.9, NO_EDGE after the phase-2 baselines is a valid, "
            "money-saving result: do not proceed to sequence nets (phase 3)."
            if verdict == "NO_EDGE"
            else "Champion(s) cleared all §12.5 gates; eligible for phase-3 "
            "comparison and phase-5 wiring (still MODEL-OUTPUT, never the oracle)."
        ),
    }
    out = models_dir / "champions.json"
    out.write_text(json.dumps(registry, indent=2, default=str))

    print(f"run {run_id}: verdict={verdict}  champions={len(champions)}  "
          f"rejected-best={len(rejected)}")
    if champions:
        for c in champions:
            print(f"  CHAMPION {c['asset']} h={c['horizon']} {c['target']} "
                  f"{c['model']}  IC-t={c['ic_tstat']:.1f} Sharpe={c['sharpe']:.2f} "
                  f"PBO={c['group_pbo']:.2f} DSR={c['dsr']:.2f}")
    else:
        print("  no model cleared the §12.5 gates — NO_EDGE recorded.")
    print(f"  registry → {out}")
    return registry


if __name__ == "__main__":
    select()
