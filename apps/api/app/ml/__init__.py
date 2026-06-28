"""ML meta-signal (PLAN §12) — leakage-safe model zoo over the terminal's signals.

**Phase 1 only (this package's current scope): the validation harness.**
Per PLAN §12.8 the build is strictly phased and *nothing* past the harness ships
until a deliberately-leaky feature is provably caught (see ``tests/test_leakage``):

  1. dataset.py  — point-in-time feature matrix (release-time joins) ............ DONE
     labels.py   — log-return / vol-scaled / triple-barrier labels ............. DONE
     cv.py       — purged K-fold + embargo, walk-forward, CPCV splitters ....... DONE
  2. zoo/ + train.py + select.py  — ElasticNet/LightGBM baselines + PBO/DSR .... NOT BUILT
  3. sequence nets (TCN/LSTM/CNN-LSTM)  ........................................ NOT BUILT
  4. stacking + calibration  ................................................... NOT BUILT
  5. predict.py daily inference job (default-OFF) + ML panel + wiring  ......... NOT BUILT

The harness is the load-bearing 80% of the deliverable; the models are 20%.
Treat every future model as guilty of overfitting until walk-forward proves
otherwise. If the phase-2 baselines show no OOS edge after costs, **that is the
valuable result** — stop there.

No torch / sklearn / lightgbm imports at this layer: phase 1 is numpy + pandas
only (those heavier deps are not yet installed in the api venv and belong to
phase 2+). Everything here is local and $0.
"""
