"""Portfolio / holdings layer (PLAN §10 #3).

The terminal's only record of *what the user actually owns*. Positions live
in SQLite (local+private, manual/CSV entry); P&L, asset-class exposure and
drift-vs-strategist are all derived at read time from cached prices, so
nothing here needs its own ingestion job.
"""

from app.portfolio.holdings import (
    BUCKET_OF_CLASS,
    compute_portfolio,
    latest_closes,
)

__all__ = ["BUCKET_OF_CLASS", "compute_portfolio", "latest_closes"]
