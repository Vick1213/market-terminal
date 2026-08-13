"""Vendored Kronos inference core (MIT) — https://github.com/shiyu-coder/Kronos

Kronos is the first open-source foundation model for financial K-lines
(candlesticks), pre-trained on 12B+ bars from 45 global exchanges
(Shi et al., arXiv:2508.02739, AAAI 2026). Weights live on HuggingFace
under ``NeoQuasar/``.

Vendored rather than pip-installed because upstream ships no package —
its README says ``from model import ...`` inside a repo checkout. Only
``model/kronos.py`` and ``model/module.py`` are copied (inference path);
the single edit is replacing upstream's ``sys.path`` hack with a relative
import. Training/finetuning code is intentionally left out.
"""

from app.forecast.kronos.kronos import Kronos, KronosPredictor, KronosTokenizer

__all__ = ["Kronos", "KronosPredictor", "KronosTokenizer"]
