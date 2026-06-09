"""POST /api/sentiment — single text, batch, and entity-localized scoring.

Backed by the shared SentimentService (FinBERT on MPS in a thread-pool, scores
cached by text-hash in DuckDB ts_sentiment). Pass ``tickers`` with a single
``text`` to get per-company scores from the entity-localized sentence windows
(PLAN §3a — encoder-first, no LLM).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api", tags=["sentiment"])

MAX_BATCH = 256


class SentimentRequest(BaseModel):
    text: str | None = None
    texts: list[str] | None = Field(default=None, max_length=MAX_BATCH)
    tickers: list[str] | None = None  # with `text`: entity-localized scoring
    use_cache: bool = True


class SentimentResult(BaseModel):
    text_hash: str
    score: float
    confidence: float
    label: str
    model: str
    cached: bool
    flagged: bool


class SentimentResponse(BaseModel):
    device: str
    result: SentimentResult | None = None
    results: list[SentimentResult] | None = None
    entities: dict[str, SentimentResult] | None = None


@router.post("/sentiment", response_model=SentimentResponse)
async def sentiment(req: SentimentRequest, request: Request) -> SentimentResponse:
    svc = request.app.state.sentiment

    if req.text and req.tickers:
        entities = await svc.score_entities(req.text, req.tickers)
        return SentimentResponse(
            device=svc.device,
            entities={t: SentimentResult(**s.as_dict()) for t, s in entities.items()},
        )

    if req.text is not None:
        s = await svc.score_text(req.text, use_cache=req.use_cache)
        return SentimentResponse(device=svc.device, result=SentimentResult(**s.as_dict()))

    if req.texts:
        scores = await svc.score_texts(req.texts, use_cache=req.use_cache)
        return SentimentResponse(
            device=svc.device, results=[SentimentResult(**s.as_dict()) for s in scores]
        )

    raise HTTPException(status_code=422, detail="provide `text` or `texts`")
