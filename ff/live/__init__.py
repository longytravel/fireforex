"""Fire Forex live-parity runner.

The whole point of Fire Forex is to prove that a parameter-swept backtest
behaves the same way in production. This package drives the live side: a
single Python process on the VPS that watches IC Markets M1 data across N
pairs, evaluates the backtest signal pipeline on every main-TF bar close,
and emits trade plans to an MT5 bridge. No MQL5 EA, no ZMQ, no DLL.

Public surface:
- :class:`ff.live.runner.LiveConfig` — input contract
- :func:`ff.live.runner.run` — entry point (blocking, respects stop_event)
- :class:`ff.live.broker_mt5.MT5Broker` — order-routing adapter
- :func:`ff.live.reconcile.reconcile` — backtest↔live trade matcher
"""
from __future__ import annotations

from . import runner  # re-export
from . import broker_mt5  # noqa: F401
from . import reconcile  # noqa: F401

__all__ = ["runner", "broker_mt5", "reconcile"]
