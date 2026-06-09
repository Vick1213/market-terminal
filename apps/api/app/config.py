"""Application settings and resolved filesystem paths.

Everything configurable lives here so the rest of the app never reaches for
os.environ directly. Values are read from the environment (prefix ``MARKET_``)
and an optional ``.env`` file in apps/api.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# apps/api/app/config.py -> parents: [0]=app [1]=api [2]=apps [3]=<repo root>
REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MARKET_",
        env_file=str(Path(__file__).resolve().parents[1] / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Market Terminal API"
    version: str = "0.1.0"

    # Contact email baked into the global User-Agent. SEC EDGAR REQUIRES a
    # descriptive UA or it returns 403; we use the same UA everywhere.
    contact_email: str = "saatvik1213@gmail.com"

    # Where DuckDB / SQLite / the HTTP cache live. Defaults to <repo>/data.
    data_dir: Path = Field(default=REPO_ROOT / "data")

    # Demo heartbeat cadence (seconds) for the Phase-0 WS round-trip.
    heartbeat_seconds: int = 5

    # --- Phase 1: sentiment + news ---
    # Optional Finnhub key (free tier, 60 calls/min). Empty -> ingestor disabled,
    # Yahoo RSS + EDGAR still run (graceful degradation per PLAN §3a).
    finnhub_api_key: str = ""

    # Optional FinBERT + distilroberta-financial ensemble (PLAN §3a accuracy-
    # without-size). Off by default: doubles load time/RAM for marginal gain.
    sentiment_ensemble: bool = False

    # Force inference device ("mps" | "cpu"); empty = auto-detect MPS.
    sentiment_device: str = ""

    # Poll cadences (minutes). Yahoo/Finnhub per-ticker RSS 5-15 min per plan;
    # EDGAR submissions refresh ~10 min server-side.
    news_poll_minutes: int = 10
    edgar_poll_minutes: int = 10
    # Only ingest EDGAR filings newer than this many days (first-run flood guard).
    edgar_lookback_days: int = 7

    # CORS origins allowed to call the API (the Next.js dev server).
    cors_origins: list[str] = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]

    @property
    def user_agent(self) -> str:
        return f"MarketTerminal/{self.version} ({self.contact_email})"

    @property
    def duckdb_path(self) -> Path:
        return self.data_dir / "market.duckdb"

    @property
    def sqlite_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def http_cache_dir(self) -> Path:
        return self.data_dir / "http_cache"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.http_cache_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
