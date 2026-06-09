"""FinBERT sentiment engine — the reusable scoring service every panel needs.

Model strategy (PLAN §3a, decided 2026-06-09): a SMALL encoder only on the bulk
path. ``ProsusAI/finbert`` (110M) does one forward pass -> 3-class softmax;
normalized score = p_positive - p_negative (-1..+1), confidence = max(softmax).
NO generative LLM anywhere on this path — token generation is 10-100x slower
per item than an encoder classification.

Design:
  * Lazy load on first request inside a single-worker ThreadPoolExecutor, so
    the FastAPI event loop never blocks on torch (MPS ops + tokenization are
    blocking). One worker also serializes GPU access.
  * Always read ``model.config.id2label`` before normalizing — finbert-tone has
    a different label order and will sign-flip if you assume indices.
  * Scores cached in DuckDB ``ts_sentiment`` keyed by text-hash, so a re-seen
    headline is never rescored.
  * Optional ensemble (PLAN "accuracy without size"): FinBERT +
    distilroberta-financial (82M), confidence-gated — labels agree -> trust;
    disagree -> keep the FinBERT score but halve confidence and flag.
  * Entity-localized per-company scoring with NO LLM: regex-extract the
    sentence window around each ticker mention and score that span.

Off-the-shelf only — do NOT fine-tune (arXiv 2512.00946: up to -30% accuracy
from catastrophic interference).

TODO(P1+): ONNX-int8 CPU copy via optimum[onnxruntime] for singleton requests
(at batch=1 MPS dispatch overhead makes it slower than CPU) and extra parallel
CPU throughput alongside MPS batches.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone

from app.db.duck import DuckStore

log = logging.getLogger("market.sentiment")

PRIMARY_MODEL = "ProsusAI/finbert"
# PLAN §3a calls this "mrm8488/distilroberta-financial"; full HF id below.
ENSEMBLE_MODEL = "mrm8488/distilroberta-finetuned-financial-news-sentiment-analysis"

# Cache rows for document-level (no specific entity) scores use this symbol key
# because ts_sentiment's PK is (text_hash, symbol) and NULLs can't be PK parts.
_NO_SYMBOL = ""

_BATCH_SIZE = 32
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def text_hash(text: str) -> str:
    """Stable cache key: hash of the whitespace-collapsed, lowercased text."""
    norm = re.sub(r"\s+", " ", text).strip().lower()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:32]


@dataclass
class SentimentScore:
    text_hash: str
    score: float       # p_positive - p_negative, -1..+1
    confidence: float  # max(softmax); ensemble-disagreement halves it
    label: str         # positive | negative | neutral
    model: str
    cached: bool = False
    flagged: bool = False  # ensemble disagreement -> candidate for escalation

    def as_dict(self) -> dict:
        return {
            "text_hash": self.text_hash,
            "score": round(self.score, 4),
            "confidence": round(self.confidence, 4),
            "label": self.label,
            "model": self.model,
            "cached": self.cached,
            "flagged": self.flagged,
        }


class SentimentService:
    def __init__(
        self,
        duck: DuckStore,
        *,
        ensemble: bool = False,
        device: str = "",
    ) -> None:
        self._duck = duck
        self._ensemble_enabled = ensemble
        self._device_override = device
        # Single worker: serializes model load + MPS access, never the event loop.
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="finbert")
        self._load_lock = threading.Lock()
        self._primary: tuple | None = None    # (tokenizer, model, id2label)
        self._secondary: tuple | None = None
        self.device: str = "unloaded"

    # ------------------------------------------------------------------ load

    def _load_bundle(self, model_id: str, device: str) -> tuple:
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        tok = AutoTokenizer.from_pretrained(model_id)
        model = AutoModelForSequenceClassification.from_pretrained(model_id)
        model = model.to(device).eval()
        # NEVER assume label order — finbert vs finbert-tone differ (sign flip).
        id2label = {int(i): lbl.lower() for i, lbl in model.config.id2label.items()}
        log.info("loaded %s on %s — id2label=%s", model_id, device, id2label)
        return tok, model, id2label

    def _ensure_loaded(self) -> None:
        """Runs inside the pool thread; idempotent."""
        with self._load_lock:
            if self._primary is not None:
                return
            import torch

            device = self._device_override or (
                "mps" if torch.backends.mps.is_available() else "cpu"
            )
            self.device = device
            self._primary = self._load_bundle(PRIMARY_MODEL, device)
            if self._ensemble_enabled:
                try:
                    self._secondary = self._load_bundle(ENSEMBLE_MODEL, device)
                except Exception:
                    log.exception("ensemble model load failed — running FinBERT-only")
                    self._secondary = None

    # ------------------------------------------------------------- inference

    def _score_with(self, bundle: tuple, texts: list[str]) -> list[tuple[float, float, str]]:
        """Forward passes in _BATCH_SIZE chunks. Pool thread only."""
        import torch

        tok, model, id2label = bundle
        out: list[tuple[float, float, str]] = []
        for i in range(0, len(texts), _BATCH_SIZE):
            chunk = texts[i : i + _BATCH_SIZE]
            enc = tok(
                chunk,
                padding=True,
                truncation=True,
                max_length=512,
                return_tensors="pt",
            ).to(model.device)
            with torch.no_grad():
                probs = torch.softmax(model(**enc).logits, dim=-1).cpu()
            for row in probs:
                p = {id2label[j]: float(row[j]) for j in range(row.shape[0])}
                score = p.get("positive", 0.0) - p.get("negative", 0.0)
                label = max(p, key=p.get)  # type: ignore[arg-type]
                out.append((score, float(row.max()), label))
        return out

    def _score_blocking(self, texts: list[str]) -> list[SentimentScore]:
        """Score texts (no cache). Pool thread only."""
        self._ensure_loaded()
        assert self._primary is not None
        primary = self._score_with(self._primary, texts)

        if self._secondary is None:
            return [
                SentimentScore(text_hash(t), s, c, lbl, "finbert")
                for t, (s, c, lbl) in zip(texts, primary)
            ]

        # Confidence-gated ensemble: agree -> average; disagree -> keep FinBERT
        # but halve confidence and flag for the optional async <=4B escalation.
        secondary = self._score_with(self._secondary, texts)
        results: list[SentimentScore] = []
        for t, (s1, c1, l1), (s2, c2, l2) in zip(texts, primary, secondary):
            if l1 == l2:
                results.append(
                    SentimentScore(
                        text_hash(t), (s1 + s2) / 2, (c1 + c2) / 2, l1, "finbert+distilroberta"
                    )
                )
            else:
                results.append(
                    SentimentScore(
                        text_hash(t), s1, c1 / 2, l1, "finbert+distilroberta", flagged=True
                    )
                )
        return results

    # ----------------------------------------------------------- cache layer

    def _cache_get(self, hashes: list[str], symbol: str) -> dict[str, SentimentScore]:
        if not hashes:
            return {}
        ph = ",".join("?" for _ in hashes)
        rows = self._duck.fetchall(
            f"SELECT text_hash, score, confidence, label, model FROM ts_sentiment "
            f"WHERE symbol = ? AND text_hash IN ({ph})",
            [symbol, *hashes],
        )
        return {
            r[0]: SentimentScore(r[0], r[1], r[2], r[3], r[4], cached=True) for r in rows
        }

    def _cache_put(self, scores: list[SentimentScore], symbol: str, source: str) -> None:
        now = datetime.now(timezone.utc)
        self._duck.executemany(
            "INSERT OR REPLACE INTO ts_sentiment "
            "(text_hash, source, symbol, ts, score, confidence, label, model) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (s.text_hash, source, symbol, now, s.score, s.confidence, s.label, s.model)
                for s in scores
            ],
        )

    # ------------------------------------------------------------ public API

    async def score_texts(
        self,
        texts: list[str],
        *,
        symbol: str | None = None,
        source: str = "api",
        use_cache: bool = True,
    ) -> list[SentimentScore]:
        """Score a batch, serving cache hits from ts_sentiment and persisting misses."""
        if not texts:
            return []
        sym_key = symbol or _NO_SYMBOL
        hashes = [text_hash(t) for t in texts]

        loop = asyncio.get_running_loop()
        cached: dict[str, SentimentScore] = {}
        if use_cache:
            cached = await loop.run_in_executor(
                None, self._cache_get, list(set(hashes)), sym_key
            )

        # Preserve order; only score each distinct missing text once.
        missing: dict[str, str] = {}
        for t, h in zip(texts, hashes):
            if h not in cached and h not in missing:
                missing[h] = t

        if missing:
            fresh = await loop.run_in_executor(
                self._pool, self._score_blocking, list(missing.values())
            )
            await loop.run_in_executor(None, self._cache_put, fresh, sym_key, source)
            cached.update({s.text_hash: s for s in fresh})

        return [cached[h] for h in hashes]

    async def score_text(self, text: str, **kw) -> SentimentScore:
        return (await self.score_texts([text], **kw))[0]

    # ------------------------------------- entity-localized scoring (no LLM)

    @staticmethod
    def entity_spans(text: str, tickers: list[str]) -> dict[str, str]:
        """Per-ticker sentence windows: every sentence mentioning the ticker
        (as a word or $-prefixed), joined. Empty dict entry omitted."""
        sentences = _SENTENCE_SPLIT.split(text)
        spans: dict[str, str] = {}
        for ticker in tickers:
            pat = re.compile(rf"(?:^|[\s(\[$]){re.escape(ticker)}(?=$|[\s)\],.!?:;'])", re.I)
            hits = [s for s in sentences if pat.search(s)]
            if hits:
                spans[ticker] = " ".join(hits)
        return spans

    async def score_entities(
        self,
        text: str,
        tickers: list[str],
        *,
        source: str = "api",
    ) -> dict[str, SentimentScore]:
        """Per-company sentiment with NO LLM (PLAN §3a): run the same fast
        encoder on each ticker's sentence window. Genuinely ambiguous spans
        come back low-confidence/flagged — the optional <=4B escalation hook
        for those lives OFF this hot path (later phase)."""
        spans = self.entity_spans(text, tickers)
        out: dict[str, SentimentScore] = {}
        for ticker, span in spans.items():
            out[ticker] = await self.score_text(span, symbol=ticker, source=source)
        return out

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
