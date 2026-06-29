"""JEPA windowing — convert the leakage-safe feature matrix into windowed
multichannel tensors for self-supervised market-STATE pretraining (PLAN §12 JEPA).

The §12 baselines already established the honest ground truth on this data: free
price/macro features forecast **volatility / regime** robustly but **returns** die
under deflation. A Joint-Embedding Predictive Architecture changes the
*representation*, not that fact — so this pipeline is framed as a market-state
encoder (regime + vol + conditioning), and any return claim built on top is held
to the same PBO/DSR/forward-holdout bar as everything else in app/ml.

This module is the DATA CONVERSION layer. It does NOT train anything. It turns
``features.build_model_matrix`` (the leakage-safe, PIT, causal, stationary
matrix) into the tensors a JEPA encoder consumes.

Leakage discipline (identical to the rest of app/ml):
  * The input is the SAME leak-gated matrix (PIT macro joins + the 4/4 leak gate
    still apply). We add NO new lookahead.
  * Per-channel normalization is **causal**: a trailing rolling z-score computed
    per (asset, channel) using only rows up to and including ``t``. No global fit,
    so no test-fold statistics bleed into training rows. (Many ``eng_*`` columns
    are already z/changes; this re-standardizes the level-ish ones to a common
    scale and is a no-op in expectation for the already-stationary ones.)
  * Windows NEVER cross an asset boundary (a window ending on name A contains only
    A's own past), and the END row of a window is its decision date ``t`` — so the
    same date-purged walk-forward + embargo that ``cross_section.DateWalkForward``
    applies downstream keeps any label out of the context.

Efficiency: we do NOT materialize overlapping windows (147 names × ~5000 days ×
L × C × 4 bytes ≈ 10 GB). We store the normalized 2D panel (R, C) ≈ 160 MB plus a
``valid_ends`` index of legal window-end row positions; the torch ``WindowDataset``
slices windows lazily in ``__getitem__``. Same tensors, ~60× less disk/RAM.

Artifacts written to ``data/ml/jepa/`` (memmap-friendly ``.npy`` + a JSON manifest):
  panel.npy      float32 (R, C)   causally-normalized features, NaN→0
  finite.npy     bool    (R, C)   True where the value was originally finite
  dates.npy      int64   (R,)     decision date (days since epoch, normalized)
  asset_id.npy   int32   (R,)     asset index into manifest["assets"]
  valid_ends.npy int64   (M,)     row positions that are legal window ends
  manifest.json  L, channels, normalization, counts, per-channel coverage

CLI:
  cd apps/api && .venv/bin/python -m app.ml.windows                # universe, L=64
  .venv/bin/python -m app.ml.windows --length 96 --start 2006-01-01
"""
from __future__ import annotations

import argparse
import json
import pickle
from pathlib import Path

import numpy as np
import pandas as pd

DEFAULT_LENGTH = 64          # context window (trading days ≈ a quarter)
DEFAULT_START = "2006-01-01"  # macro coverage floor (matches xtrain)
DEFAULT_NORM_WIN = 252
DEFAULT_NORM_MINP = 63
# A window is legal only if its rows are "finite enough" originally — early
# warmup windows (mostly-NaN macro) carry no state and would just teach the
# encoder to model the impute value.
DEFAULT_MIN_FINITE = 0.60


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


# --- causal normalization ---------------------------------------------------


def causal_zscore(
    matrix: pd.DataFrame, win: int = DEFAULT_NORM_WIN, minp: int = DEFAULT_NORM_MINP
) -> pd.DataFrame:
    """Per-(asset, channel) TRAILING rolling z-score. Causal → PIT-safe.

    ``matrix`` is MultiIndex[date, asset]. For each asset's contiguous date
    series, z = (x - rolling_mean) / rolling_std over a backward window ending at
    ``t``. Leaves NaN where the trailing window is too short; the caller fills.
    """
    out = []
    for asset, g in matrix.groupby(level="asset", sort=False):
        gg = g.sort_index()
        mu = gg.rolling(win, min_periods=minp).mean()
        sd = gg.rolling(win, min_periods=minp).std()
        z = (gg - mu) / sd.replace(0.0, np.nan)
        out.append(z)
    return pd.concat(out).reindex(matrix.index)


# --- panel materialization --------------------------------------------------


def build_panel(
    matrix: pd.DataFrame,
    *,
    length: int = DEFAULT_LENGTH,
    norm_win: int = DEFAULT_NORM_WIN,
    norm_minp: int = DEFAULT_NORM_MINP,
    min_finite: float = DEFAULT_MIN_FINITE,
    clip: float = 8.0,
) -> dict:
    """Turn a (date, asset) feature matrix into the JEPA panel arrays.

    Returns a dict of numpy arrays + metadata (see module docstring). The heavy
    lifting is: sort by (asset, date), causal-z each channel, record the
    originally-finite mask, fill NaN→0, and compute the legal window-end row
    positions (≥ ``length`` of same-asset history AND ≥ ``min_finite`` finite
    fraction in the window).
    """
    channels = list(matrix.columns)
    C = len(channels)

    # Stable (asset, date) ordering so each asset is a contiguous block.
    m = matrix.copy()
    m = m.reset_index().sort_values(["asset", "date"]).set_index(["date", "asset"])
    finite = np.isfinite(m.to_numpy(dtype=float))  # original finiteness, pre-norm

    z = causal_zscore(m, norm_win, norm_minp)
    panel = np.ascontiguousarray(z.to_numpy(dtype=np.float32))  # writable copy
    np.nan_to_num(panel, copy=False, nan=0.0, posinf=0.0, neginf=0.0)
    np.clip(panel, -clip, clip, out=panel)  # bound causal-z tails for SSL stability

    dates = m.index.get_level_values("date").to_numpy()
    assets_str = m.index.get_level_values("asset").to_numpy()
    asset_names = sorted(pd.unique(assets_str).tolist())
    aid_map = {a: i for i, a in enumerate(asset_names)}
    asset_id = np.array([aid_map[a] for a in assets_str], dtype=np.int32)
    # days since epoch, robust to the source datetime64 unit (us/ns).
    date_epoch = pd.DatetimeIndex(dates).normalize().values.astype("datetime64[D]").astype(np.int64)

    # Legal window ends: position p is legal iff rows [p-length+1, p] are all the
    # SAME asset (contiguous block, no boundary crossing) and the window's overall
    # finite fraction ≥ min_finite. finite fraction uses a cumulative-sum trick.
    R = len(panel)
    fin_per_row = finite.mean(axis=1)  # fraction of channels finite at each row
    csum = np.concatenate([[0.0], np.cumsum(fin_per_row)])
    valid_ends = []
    # contiguous asset blocks
    starts = np.r_[0, np.where(np.diff(asset_id) != 0)[0] + 1, R]
    for b in range(len(starts) - 1):
        lo, hi = starts[b], starts[b + 1]
        for p in range(lo + length - 1, hi):
            frac = (csum[p + 1] - csum[p + 1 - length]) / length
            if frac >= min_finite:
                valid_ends.append(p)
    valid_ends = np.asarray(valid_ends, dtype=np.int64)

    return {
        "panel": panel,
        "finite": finite,
        "dates": date_epoch,
        "asset_id": asset_id,
        "valid_ends": valid_ends,
        "channels": channels,
        "assets": asset_names,
        "length": length,
        "norm_win": norm_win,
        "norm_minp": norm_minp,
        "min_finite": min_finite,
        "n_channels": C,
    }


def save_panel(out_dir: Path, p: dict) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    np.save(out_dir / "panel.npy", p["panel"])
    np.save(out_dir / "finite.npy", p["finite"])
    np.save(out_dir / "dates.npy", p["dates"])
    np.save(out_dir / "asset_id.npy", p["asset_id"])
    np.save(out_dir / "valid_ends.npy", p["valid_ends"])
    coverage = {c: float(p["finite"][:, i].mean()) for i, c in enumerate(p["channels"])}
    manifest = {
        "length": p["length"],
        "n_rows": int(p["panel"].shape[0]),
        "n_channels": p["n_channels"],
        "n_windows": int(len(p["valid_ends"])),
        "n_assets": len(p["assets"]),
        "norm_win": p["norm_win"],
        "norm_minp": p["norm_minp"],
        "min_finite": p["min_finite"],
        "channels": p["channels"],
        "assets": p["assets"],
        "coverage": coverage,
        "date_min": int(p["dates"].min()),
        "date_max": int(p["dates"].max()),
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def report(manifest: dict, dates: np.ndarray, valid_ends: np.ndarray) -> None:
    print("=== JEPA panel readiness report ===")
    print(f"  rows (asset-days):   {manifest['n_rows']:,}")
    print(f"  channels:            {manifest['n_channels']}")
    print(f"  assets:              {manifest['n_assets']}")
    print(f"  window length L:     {manifest['length']}")
    print(f"  legal windows:       {manifest['n_windows']:,}  "
          f"({100*manifest['n_windows']/max(manifest['n_rows'],1):.0f}% of rows)")
    d0 = pd.Timestamp(int(manifest['date_min']), unit="D").date()
    d1 = pd.Timestamp(int(manifest['date_max']), unit="D").date()
    print(f"  date span:           {d0} → {d1}")
    end_dates = pd.DatetimeIndex(pd.to_datetime(dates[valid_ends], unit="D"))
    by_yr = pd.Series(1, index=end_dates).resample("YE").sum()
    print("  windows / year (first/last 3):",
          {int(k.year): int(v) for k, v in list(by_yr.items())[:3]}, "...",
          {int(k.year): int(v) for k, v in list(by_yr.items())[-3:]})
    cov = manifest["coverage"]
    worst = sorted(cov.items(), key=lambda kv: kv[1])[:8]
    print("  lowest-coverage channels:")
    for c, v in worst:
        print(f"    {c:28s} {100*v:5.1f}% finite")


# --- matrix loading (reuses the xtrain cross-sectional cache) ---------------


def load_model_matrix(start: str, uni_db: Path | None = None) -> pd.DataFrame:
    """Build (or load from the xtrain pickle cache) the leak-safe model matrix
    over the wide universe. Mirrors xtrain's cache key so we hit the existing
    ``.xs_matrix_<mtime>_<start>.pkl`` without a rebuild."""
    import duckdb

    from app.ml.cross_section import RoutingDuck
    from app.ml.features import build_model_matrix

    repo = _repo_root()
    main_db = repo / "data" / "market.duckdb"
    uni_db = Path(uni_db) if uni_db else repo / "data" / "ml" / "universe.duckdb"
    if not uni_db.exists():
        raise SystemExit(f"universe DB missing: {uni_db} (run scratchpad/fetch_universe.py)")

    cache = uni_db.parent / f".xs_matrix_{int(uni_db.stat().st_mtime)}_{start}.pkl"
    if cache.exists():
        print(f"[cache] loading matrix from {cache.name}", flush=True)
        return pickle.loads(cache.read_bytes())

    print("[build] assembling PIT matrix over universe (one-time, ~min)...", flush=True)
    main = duckdb.connect(str(main_db), read_only=True)
    uni = duckdb.connect(str(uni_db), read_only=True)
    symbols = [r[0] for r in uni.execute(
        "SELECT DISTINCT symbol FROM ts_price WHERE source='yahoo'").fetchall()]
    duck = RoutingDuck(main, uni)
    matrix, _ = build_model_matrix(duck, assets=tuple(symbols))
    matrix = matrix[matrix.index.get_level_values("date") >= pd.Timestamp(start)]
    matrix = matrix.dropna(axis=1, how="all")
    main.close(); uni.close()
    cache.write_bytes(pickle.dumps(matrix))
    print(f"[build] cached matrix {matrix.shape} -> {cache.name}", flush=True)
    return matrix


# --- torch dataset (lazy windowing) ----------------------------------------


def make_dataset(jepa_dir: Path):
    """Return a torch Dataset over the materialized panel. Imported lazily so the
    converter has no hard torch dependency. Each item is a (L, C) float32 window
    ending on a legal decision date, plus its finite-mask and (date, asset_id)."""
    import torch

    panel = np.load(jepa_dir / "panel.npy", mmap_mode="r")
    finite = np.load(jepa_dir / "finite.npy", mmap_mode="r")
    dates = np.load(jepa_dir / "dates.npy")
    asset_id = np.load(jepa_dir / "asset_id.npy")
    ends = np.load(jepa_dir / "valid_ends.npy")
    L = json.loads((jepa_dir / "manifest.json").read_text())["length"]

    class WindowDataset(torch.utils.data.Dataset):
        def __len__(self):
            return len(ends)

        def __getitem__(self, i):
            p = int(ends[i])
            sl = slice(p - L + 1, p + 1)
            x = torch.from_numpy(np.ascontiguousarray(panel[sl]).astype(np.float32))
            mask = torch.from_numpy(np.ascontiguousarray(finite[sl]))
            return {
                "x": x,                              # (L, C)
                "mask": mask,                        # (L, C) bool, original finiteness
                "date": int(dates[p]),
                "asset_id": int(asset_id[p]),
            }

    return WindowDataset()


def _main() -> int:
    ap = argparse.ArgumentParser(description="Materialize JEPA windows from the leak-safe matrix")
    ap.add_argument("--length", type=int, default=DEFAULT_LENGTH)
    ap.add_argument("--start", default=DEFAULT_START)
    ap.add_argument("--min-finite", type=float, default=DEFAULT_MIN_FINITE)
    ap.add_argument("--uni-db", default=None)
    ap.add_argument("--out", default=None, help="output dir (default data/ml/jepa)")
    a = ap.parse_args()

    matrix = load_model_matrix(a.start, a.uni_db)
    print(f"matrix {matrix.shape}  "
          f"{matrix.index.get_level_values('asset').nunique()} names "
          f"{matrix.index.get_level_values('date').min().date()}->"
          f"{matrix.index.get_level_values('date').max().date()}", flush=True)
    p = build_panel(matrix, length=a.length, min_finite=a.min_finite)
    out_dir = Path(a.out) if a.out else _repo_root() / "data" / "ml" / "jepa"
    manifest = save_panel(out_dir, p)
    report(manifest, p["dates"], p["valid_ends"])
    print(f"\nwrote → {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
