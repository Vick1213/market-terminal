"""TS-JEPA — a Joint-Embedding Predictive Architecture for market STATE (PLAN §12).

Self-supervised pretraining over the windowed panel from ``windows.py``. The
objective is the JEPA objective adapted to 1-D market time: from a CONTEXT block
(the earlier days of a window) predict, IN LATENT SPACE, the representation of a
future TARGET block (the later days) — never the raw prices. A stop-grad EMA
"target encoder" provides the prediction targets, so the model can't collapse to
reconstructing noise (LeCun's I-JEPA recipe, future-block variant). Predicting the
future block's latent *is* "predict the next market state", which is the point.

Why JEPA and not a plain forecaster: the §12 falsification battery already showed
raw return forecasting on this data dies under deflation. JEPA learns a
*representation* of the market window; the honest bet (per §12) is that this
representation captures vol/regime structure well. Whether it also yields a
deflation-surviving return signal is then tested in the eval phase — held to the
SAME PBO/DSR/forward-holdout bar as everything else.

Leakage discipline: the windows are built from the leak-gated causal matrix
(``windows.py``), never cross an asset boundary, and the target block is strictly
AFTER the context within the same window — so "predicting the future block" uses
only information that, at the context's last day, is genuinely unknown. The
encoder is pretrained self-supervised (no labels), so there is no label leak; the
downstream probes apply the date-purged walk-forward on top.

Run (smoke):  cd apps/api && .venv/bin/python -m app.ml.jepa --steps 100
Run (full):   .venv/bin/python -m app.ml.jepa --epochs 8
Embed:        .venv/bin/python -m app.ml.jepa --embed   # writes embeddings.npy
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

JEPA_DIR = Path(__file__).resolve().parents[4] / "data" / "ml" / "jepa"


class WindowDataset(torch.utils.data.Dataset):
    """Lazy windowing over the materialized panel. Top-level (picklable) so the
    DataLoader can spawn workers. Each item is a (L, C) window ending on a legal
    decision date + its original-finiteness mask and (date, asset_id) keys."""

    def __init__(self, jepa_dir: Path = JEPA_DIR):
        self.dir = Path(jepa_dir)
        self.L = json.loads((self.dir / "manifest.json").read_text())["length"]
        self._panel = self._finite = None  # opened per-worker (memmap not fork-safe)
        self.dates = np.load(self.dir / "dates.npy")
        self.asset_id = np.load(self.dir / "asset_id.npy")
        self.ends = np.load(self.dir / "valid_ends.npy")

    def _ensure(self):
        if self._panel is None:
            self._panel = np.load(self.dir / "panel.npy", mmap_mode="r")
            self._finite = np.load(self.dir / "finite.npy", mmap_mode="r")

    def __len__(self):
        return len(self.ends)

    def __getitem__(self, i):
        self._ensure()
        p = int(self.ends[i]); sl = slice(p - self.L + 1, p + 1)
        return {
            "x": torch.from_numpy(np.ascontiguousarray(self._panel[sl]).astype(np.float32)),
            "mask": torch.from_numpy(np.ascontiguousarray(self._finite[sl])),
            "date": int(self.dates[p]),
            "asset_id": int(self.asset_id[p]),
        }


def _device(pref: str = "auto") -> torch.device:
    if pref == "cpu":
        return torch.device("cpu")
    if torch.backends.mps.is_available():
        return torch.device("mps")
    if torch.cuda.is_available():
        return torch.device("cuda")
    return torch.device("cpu")


# --- model ------------------------------------------------------------------


class _PosEnc(nn.Module):
    """Learnable per-position embedding over the L day-tokens."""

    def __init__(self, length: int, d_model: int):
        super().__init__()
        self.pos = nn.Parameter(torch.zeros(1, length, d_model))
        nn.init.trunc_normal_(self.pos, std=0.02)

    def forward(self, x):  # x: (B, T, d) for the first T positions
        return x + self.pos[:, : x.size(1)]


class DayTokenEncoder(nn.Module):
    """Each trading day is a token: linear-embed its C channels, add a position
    embedding, then a small Transformer encoder. Returns per-token reps (B,T,d)."""

    def __init__(self, n_channels: int, length: int, d_model: int = 128,
                 n_layers: int = 4, n_heads: int = 4, dropout: float = 0.1):
        super().__init__()
        self.embed = nn.Linear(n_channels, d_model)
        self.pos = _PosEnc(length, d_model)
        layer = nn.TransformerEncoderLayer(
            d_model, n_heads, dim_feedforward=4 * d_model, dropout=dropout,
            batch_first=True, activation="gelu", norm_first=True)
        self.tr = nn.TransformerEncoder(layer, n_layers)
        self.norm = nn.LayerNorm(d_model)

    def forward(self, x, idx=None):
        # x: (B, T, C). idx (optional) selects a sub-sequence of positions; the
        # position embedding still uses each token's ORIGINAL index so context and
        # target share a coordinate frame.
        h = self.embed(x)
        if idx is None:
            h = self.pos(h)
        else:
            h = h + self.pos.pos[:, idx]
        return self.norm(self.tr(h))


class Predictor(nn.Module):
    """From the context summary + a target position embedding, predict that
    target token's representation (in latent space). Lightweight MLP — the heavy
    modelling is in the encoder."""

    def __init__(self, length: int, d_model: int = 128, hidden: int = 256):
        super().__init__()
        self.tgt_pos = _PosEnc(length, d_model)
        self.net = nn.Sequential(
            nn.Linear(2 * d_model, hidden), nn.GELU(),
            nn.Linear(hidden, hidden), nn.GELU(),
            nn.Linear(hidden, d_model))

    def forward(self, ctx_summary, tgt_idx):
        # ctx_summary: (B, d). tgt_idx: (k,) target positions.
        B, d = ctx_summary.shape
        k = len(tgt_idx)
        q = self.tgt_pos.pos[:, tgt_idx].expand(B, k, d)             # (B,k,d)
        s = ctx_summary.unsqueeze(1).expand(B, k, d)                 # (B,k,d)
        return self.net(torch.cat([s, q], dim=-1))                  # (B,k,d)


class Decoder(nn.Module):
    """Reconstruct the input window (L, C) from the pooled market-state z.

    This is the "understanding" probe: trained with a STOP-GRAD on the latent, its
    reconstruction error measures how much of the data the (frozen-at-each-step)
    JEPA representation actually retains. A low loss / high R² means z encodes the
    window; a high loss on, say, the vol channels would confirm the representation
    threw that information away. Per-position MLP conditioned on z + a position
    embedding (mirrors the predictor — keeps the heavy lifting in the encoder)."""

    def __init__(self, length: int, n_channels: int, d_model: int = 128, hidden: int = 256):
        super().__init__()
        self.length = length
        self.pos = _PosEnc(length, d_model)
        self.net = nn.Sequential(
            nn.Linear(2 * d_model, hidden), nn.GELU(),
            nn.Linear(hidden, hidden), nn.GELU(),
            nn.Linear(hidden, n_channels))

    def forward(self, z):
        # z: (B, d) → reconstructed window (B, L, C).
        B, d = z.shape
        pos = self.pos.pos.expand(B, self.length, d)                 # (B,L,d)
        s = z.unsqueeze(1).expand(B, self.length, d)                 # (B,L,d)
        return self.net(torch.cat([s, pos], dim=-1))                # (B,L,C)


class TSJEPA(nn.Module):
    def __init__(self, n_channels: int, length: int, pred_len: int = 16,
                 d_model: int = 128, **kw):
        super().__init__()
        self.length = length
        self.pred_len = pred_len
        self.ctx_len = length - pred_len
        self.context = DayTokenEncoder(n_channels, length, d_model, **kw)
        self.target = DayTokenEncoder(n_channels, length, d_model, **kw)
        self.predictor = Predictor(length, d_model)
        self.target.load_state_dict(self.context.state_dict())
        for p in self.target.parameters():
            p.requires_grad_(False)

    @torch.no_grad()
    def ema_update(self, m: float = 0.996):
        for tp, cp in zip(self.target.parameters(), self.context.parameters()):
            tp.mul_(m).add_(cp, alpha=1 - m)

    def forward(self, x):
        # x: (B, L, C). Context = first ctx_len days; target = last pred_len days.
        ctx_idx = torch.arange(self.ctx_len, device=x.device)
        tgt_idx = torch.arange(self.ctx_len, self.length, device=x.device)
        ctx = self.context(x[:, : self.ctx_len, :], idx=ctx_idx)      # (B,ctx,d)
        ctx_summary = ctx.mean(dim=1)                                 # (B,d)
        with torch.no_grad():
            tgt_full = self.target(x)                                 # (B,L,d)
            tgt = tgt_full[:, self.ctx_len :, :]                      # (B,k,d) stop-grad
        pred = self.predictor(ctx_summary, tgt_idx)                  # (B,k,d)
        # Smooth-L1 in latent space; normalize targets to unit-var per dim to
        # discourage the trivial constant solution.
        tgt = F.layer_norm(tgt, (tgt.size(-1),))
        return F.smooth_l1_loss(pred, tgt)

    @torch.no_grad()
    def embed(self, x):
        """Market-state representation z_t = mean-pooled FULL-window context-encoder
        tokens (deterministic, eval-time)."""
        return self.context(x).mean(dim=1)


# --- training ---------------------------------------------------------------


def train(epochs: int = 8, steps: int | None = None, batch: int = 256,
          lr: float = 3e-4, ema: float = 0.996, d_model: int = 128,
          pred_len: int = 16, n_layers: int = 4, device_pref: str = "auto",
          out: Path | None = None, seed: int = 0) -> Path:
    torch.manual_seed(seed); np.random.seed(seed)
    dev = _device(device_pref)
    man = json.loads((JEPA_DIR / "manifest.json").read_text())
    C, L = man["n_channels"], man["length"]
    ds = WindowDataset(JEPA_DIR)
    dl = torch.utils.data.DataLoader(ds, batch_size=batch, shuffle=True,
                                     num_workers=4, drop_last=True, persistent_workers=True)
    model = TSJEPA(C, L, pred_len=pred_len, d_model=d_model, n_layers=n_layers).to(dev)
    opt = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad],
                            lr=lr, weight_decay=0.04)
    n_steps = steps if steps else epochs * len(dl)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=lr, total_steps=n_steps,
                                                pct_start=0.1)
    print(f"device={dev}  windows={len(ds):,}  steps/epoch={len(dl)}  "
          f"target_steps={n_steps}  C={C} L={L} pred_len={pred_len} d={d_model}", flush=True)
    model.train()
    step = 0; ema_loss = None; done = False
    for ep in range(epochs if not steps else 10_000):
        for b in dl:
            x = b["x"].to(dev, non_blocking=True)
            loss = model(x)
            opt.zero_grad(); loss.backward()
            nn.utils.clip_grad_norm_(model.context.parameters(), 1.0)
            opt.step(); sched.step(); model.ema_update(ema)
            l = float(loss.detach().cpu())
            ema_loss = l if ema_loss is None else 0.98 * ema_loss + 0.02 * l
            if step % 20 == 0:
                print(f"  step {step:5d}/{n_steps}  loss {l:.4f}  ema {ema_loss:.4f}", flush=True)
            step += 1
            if step >= n_steps:
                done = True; break
        if done:
            break
    out = out or (JEPA_DIR / "encoder.pt")
    torch.save({"state_dict": model.state_dict(), "C": C, "L": L,
                "pred_len": pred_len, "d_model": d_model, "n_layers": n_layers,
                "final_ema_loss": ema_loss}, out)
    print(f"saved encoder → {out}  (final ema loss {ema_loss:.4f})")
    return out


def posttrain(steps: int | None = None, epochs: int = 2, batch: int = 256,
              lr: float = 1e-4, dec_lr: float = 3e-4, ema: float = 0.998,
              recon_w: float = 1.0, device_pref: str = "auto",
              ckpt: Path | None = None, seed: int = 0) -> Path:
    """Post-train: continue the JEPA masked-future-latent objective on the encoder
    AND jointly train a Decoder to reconstruct the data from the market-state z.

    The two heads are cleanly separated by a stop-grad: the JEPA loss trains the
    encoder/predictor (same objective as pretraining, lower LR), while the decoder
    learns to invert the *current* representation (z is detached before the
    decoder) — so the reported reconstruction loss is an honest measure of how much
    the JEPA embedding already encodes, not the decoder bending the encoder to be
    invertible. Both losses are logged; a final eval-mode pass reports masked
    reconstruction R² overall and per-channel (so we can see WHAT z retains — e.g.
    whether the vol channels survive)."""
    torch.manual_seed(seed); np.random.seed(seed)
    dev = _device(device_pref)
    ckpt = ckpt or (JEPA_DIR / "encoder.pt")
    man = json.loads((JEPA_DIR / "manifest.json").read_text())
    C, L, channels = man["n_channels"], man["length"], man["channels"]
    c = torch.load(ckpt, map_location=dev)
    model = TSJEPA(c["C"], c["L"], pred_len=c["pred_len"], d_model=c["d_model"],
                   n_layers=c["n_layers"]).to(dev)
    model.load_state_dict(c["state_dict"])
    d_model = c["d_model"]
    decoder = Decoder(L, C, d_model=d_model).to(dev)

    ds = WindowDataset(JEPA_DIR)
    dl = torch.utils.data.DataLoader(ds, batch_size=batch, shuffle=True, num_workers=4,
                                     drop_last=True, persistent_workers=True)
    opt_e = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad],
                              lr=lr, weight_decay=0.04)
    opt_d = torch.optim.AdamW(decoder.parameters(), lr=dec_lr, weight_decay=0.01)
    n_steps = steps if steps else epochs * len(dl)
    print(f"device={dev}  warm-start {ckpt.name} (pretrain loss {c.get('final_ema_loss')})  "
          f"windows={len(ds):,}  target_steps={n_steps}  recon_w={recon_w}", flush=True)
    model.train(); decoder.train()
    step = 0; ej = er = None; done = False
    for _ in range(epochs if not steps else 10_000):
        for b in dl:
            x = b["x"].to(dev, non_blocking=True)
            m = b["mask"].to(dev, non_blocking=True).float()
            # --- JEPA head (trains encoder + predictor) ---
            loss_j = model(x)
            # --- decoder head (stop-grad on z → understanding probe) ---
            with torch.no_grad():
                z = model.context(x).mean(dim=1)            # current market-state, detached
            recon = decoder(z)
            sl = F.smooth_l1_loss(recon, x, reduction="none")
            loss_r = (sl * m).sum() / m.sum().clamp(min=1.0)
            loss = loss_j + recon_w * loss_r
            opt_e.zero_grad(); opt_d.zero_grad()
            loss.backward()
            nn.utils.clip_grad_norm_(model.context.parameters(), 1.0)
            opt_e.step(); opt_d.step(); model.ema_update(ema)
            lj, lr_ = float(loss_j.detach().cpu()), float(loss_r.detach().cpu())
            ej = lj if ej is None else 0.98 * ej + 0.02 * lj
            er = lr_ if er is None else 0.98 * er + 0.02 * lr_
            if step % 20 == 0:
                print(f"  step {step:5d}/{n_steps}  jepa {lj:.4f} (ema {ej:.4f})  "
                      f"recon {lr_:.4f} (ema {er:.4f})", flush=True)
            step += 1
            if step >= n_steps:
                done = True; break
        if done:
            break

    # --- final understanding report: masked R² overall + per-channel ---
    model.eval(); decoder.eval()
    sse = np.zeros(C); sx = np.zeros(C); sxx = np.zeros(C); n = np.zeros(C)
    with torch.no_grad():
        for bi, b in enumerate(torch.utils.data.DataLoader(ds, batch_size=512, num_workers=4)):
            if bi >= 60:
                break
            x = b["x"].to(dev); m = b["mask"].to(dev).float()
            recon = decoder(model.context(x).mean(dim=1))
            e2 = ((recon - x) ** 2 * m).sum(dim=(0, 1)).cpu().numpy()
            sse += e2
            sx += (x * m).sum(dim=(0, 1)).cpu().numpy()
            sxx += (x * x * m).sum(dim=(0, 1)).cpu().numpy()
            n += m.sum(dim=(0, 1)).cpu().numpy()
    n = np.clip(n, 1, None)
    var = sxx / n - (sx / n) ** 2
    mse = sse / n
    r2 = 1.0 - mse / np.clip(var, 1e-9, None)
    overall_r2 = float(1.0 - sse.sum() / np.clip((sxx - sx * sx / n).sum(), 1e-9, None))
    overall_recon = float(np.average(mse, weights=n))
    print("\n=== DECODER UNDERSTANDING (masked reconstruction of the data from z) ===")
    print(f"  final JEPA latent loss (ema):     {ej:.4f}")
    print(f"  final decoder recon loss (ema):   {er:.4f}   [smooth-L1, finite entries]")
    print(f"  overall reconstruction MSE:       {overall_recon:.4f}  (z-scored data, var≈1)")
    print(f"  overall reconstruction R²:        {overall_r2:.3f}  "
          f"(frac of finite-entry variance z explains)")
    order = np.argsort(-r2)
    print("  best-reconstructed channels (z retains these):")
    for i in order[:8]:
        print(f"    {channels[i]:28s} R²={r2[i]:+.2f}  (cov {100*n[i]/n.max():.0f}%)")
    print("  worst-reconstructed channels (z discards these):")
    for i in order[-8:]:
        print(f"    {channels[i]:28s} R²={r2[i]:+.2f}  (cov {100*n[i]/n.max():.0f}%)")
    # spotlight the vol channels — the open question from the first eval
    vol_ch = [j for j, ch in enumerate(channels)
              if any(k in ch for k in ("vix", "move", "rvol", "vol"))]
    if vol_ch:
        print("  vol-channel reconstruction (the vol-level question):")
        for j in vol_ch:
            print(f"    {channels[j]:28s} R²={r2[j]:+.2f}")

    out = JEPA_DIR / "encoder_posttrained.pt"
    torch.save({"state_dict": model.state_dict(), "C": C, "L": L,
                "pred_len": c["pred_len"], "d_model": d_model, "n_layers": c["n_layers"],
                "final_ema_loss": ej, "recon_ema_loss": er,
                "recon_r2": overall_r2}, out)
    torch.save({"state_dict": decoder.state_dict(), "L": L, "C": C, "d_model": d_model},
               JEPA_DIR / "decoder.pt")
    print(f"\nsaved post-trained encoder → {out}  + decoder.pt "
          f"(JEPA {ej:.4f} | recon {er:.4f} | R² {overall_r2:.3f})")
    return out


@torch.no_grad()
def embed_all(ckpt: Path | None = None, batch: int = 512, device_pref: str = "auto",
              out: Path | None = None) -> Path:
    """Encode every legal window → market-state embeddings, saved aligned to the
    valid_ends (so date/asset keys join straight back for the eval probes)."""
    dev = _device(device_pref)
    ckpt = ckpt or (JEPA_DIR / "encoder.pt")
    c = torch.load(ckpt, map_location=dev)
    model = TSJEPA(c["C"], c["L"], pred_len=c["pred_len"], d_model=c["d_model"],
                   n_layers=c["n_layers"]).to(dev)
    model.load_state_dict(c["state_dict"]); model.eval()
    ds = WindowDataset(JEPA_DIR)
    dl = torch.utils.data.DataLoader(ds, batch_size=batch, shuffle=False, num_workers=4)
    Z, dates, aids = [], [], []
    for b in dl:
        z = model.embed(b["x"].to(dev)).cpu().numpy()
        Z.append(z); dates.append(b["date"].numpy()); aids.append(b["asset_id"].numpy())
    Z = np.concatenate(Z).astype(np.float32)
    dates = np.concatenate(dates); aids = np.concatenate(aids)
    out = out or (JEPA_DIR / "embeddings.npy")
    np.save(out, Z)
    np.save(JEPA_DIR / "embed_dates.npy", dates)
    np.save(JEPA_DIR / "embed_asset_id.npy", aids)
    print(f"embeddings {Z.shape} → {out}")
    return out


def _main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=int, default=8)
    ap.add_argument("--steps", type=int, default=None, help="hard step cap (smoke test)")
    ap.add_argument("--batch", type=int, default=256)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--pred-len", type=int, default=16)
    ap.add_argument("--d-model", type=int, default=128)
    ap.add_argument("--n-layers", type=int, default=4)
    ap.add_argument("--device", default="auto")
    ap.add_argument("--embed", action="store_true", help="only embed from saved encoder")
    ap.add_argument("--embed-posttrain", action="store_true",
                    help="embed from encoder_posttrained.pt → embeddings_pt.npy")
    ap.add_argument("--posttrain", action="store_true",
                    help="warm-start encoder.pt, continue JEPA + train a reconstruction decoder")
    ap.add_argument("--recon-w", type=float, default=1.0)
    a = ap.parse_args()
    if a.embed:
        embed_all(device_pref=a.device)
        return 0
    if a.embed_posttrain:
        embed_all(ckpt=JEPA_DIR / "encoder_posttrained.pt",
                  out=JEPA_DIR / "embeddings_pt.npy", device_pref=a.device)
        return 0
    if a.posttrain:
        posttrain(epochs=a.epochs, steps=a.steps, batch=a.batch, recon_w=a.recon_w,
                  device_pref=a.device)
        return 0
    train(epochs=a.epochs, steps=a.steps, batch=a.batch, lr=a.lr,
          pred_len=a.pred_len, d_model=a.d_model, n_layers=a.n_layers,
          device_pref=a.device)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
