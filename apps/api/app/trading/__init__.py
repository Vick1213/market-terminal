"""Phase 12 — paper trading bot.

Turns the strategist's suggested allocation into Alpaca PAPER orders behind
hard, code-enforced guardrails. Three pieces:

  * broker.py     — AlpacaPaperBroker: account/positions/orders against the
                    paper endpoint, with a hard block on live trading.
  * guardrails.py — pure functions that gate every proposed order (allowlist,
                    position caps, daily-loss halt, buying-power, dust floor).
  * bot.py        — TradingBotService: read strategist + broker (ground truth),
                    diff to targets, emit proposals, and (only on the human
                    gate or explicit auto mode) submit to the paper account.

Design rules, straight from the recon on real-vs-hype Claude trading bots:
  - NEVER autonomous against a live key (broker refuses non-paper base URLs).
  - Limits enforced in code, not prompts.
  - The broker is the source of truth; the local ledger is reconciled FROM it.
"""
