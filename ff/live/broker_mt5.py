"""MT5 broker bridge — thin wrapper over the ``MetaTrader5`` pip package.

One Python process, one MT5 terminal session, both running on the same VPS.
Zero MQL5 Expert Advisor; orders go direct via ``mt5.order_send``. This
keeps the signal source inside Fire Forex and removes the translation layer
where parity could silently drift.

Windows-only (the ``MetaTrader5`` package is a Windows MetaQuotes binary).
On dev boxes without MT5 installed, import succeeds but ``connect()`` raises.

Failure-mode taxonomy (per the plan):

    ================  ==================================  ==================
    Failure            Detection                           Action
    ================  ==================================  ==================
    REQUOTE           retcode == TRADE_RETCODE_REQUOTE    Retry once with
                                                          deviation * 2; then
                                                          abort plan
    REJECT            retcode != TRADE_RETCODE_DONE       Abort; log
    Partial fill      result.volume < request.volume     Record partial as
                                                          the position size
    Disconnect        ``mt5.initialize()`` false later    status="degraded"
    Clock drift       symbol_info_tick().time vs now      WARN > 2s, ABORT
                                                          new plans > 10s
    Duplicate plan    plan_id present in tickets.jsonl    Skip
    ================  ==================================  ==================

No silent retries. Every outcome is logged.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd


LOG = logging.getLogger(__name__)


# Lazy import — MetaTrader5 is Windows-only and not available on dev boxes.
_mt5: Any = None


def _require_mt5() -> Any:
    global _mt5
    if _mt5 is None:
        try:
            import MetaTrader5 as mt5  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "MetaTrader5 pip package not installed — "
                "this is a Windows-only VPS dependency"
            ) from exc
        _mt5 = mt5
    return _mt5


@dataclass
class Ticket:
    plan_id: str
    ticket: int
    submitted_at: str
    filled_at: str | None
    fill_price: float
    fill_volume: float
    retcode: int
    comment: str


class MT5Broker:
    """One connected MT5 terminal + a thread-safe submit path.

    The runner holds a single ``MT5Broker`` instance shared across pairs. All
    order-mutating calls (submit/modify/close) are expected to run from the
    main runner thread; the package's own reads are thread-safe but writes
    must serialise.
    """

    def __init__(self, cfg: Any) -> None:
        self.cfg = cfg
        self._connected = False

    # ---------------------------------------------------------------- setup
    def connect(self) -> None:
        mt5 = _require_mt5()
        kwargs: dict[str, Any] = {
            "login": int(self.cfg.login),
            "password": str(self.cfg.password),
            "server": str(self.cfg.server),
        }
        if self.cfg.terminal_path:
            kwargs["path"] = str(self.cfg.terminal_path)
        if not mt5.initialize(**kwargs):
            err = mt5.last_error()
            raise RuntimeError(f"MT5 initialize failed: {err}")
        self._connected = True
        LOG.info("[mt5] connected as login=%s server=%s", self.cfg.login, self.cfg.server)

    def disconnect(self) -> None:
        if not self._connected:
            return
        mt5 = _require_mt5()
        mt5.shutdown()
        self._connected = False

    # ------------------------------------------------------------- market data
    def copy_rates_m1(self, pair: str, n_bars: int) -> pd.DataFrame:
        """Return the last ``n_bars`` closed M1 bars for ``pair``.

        Maps the Fire Forex pair symbol (``EUR_USD``) through
        ``symbol_map`` to the broker's symbol (``EURUSD`` / ``EURUSD.a``).
        """
        mt5 = _require_mt5()
        symbol = self.cfg.symbol_map.get(pair, pair.replace("_", ""))
        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 0, n_bars)
        if rates is None or len(rates) == 0:
            return pd.DataFrame()
        df = pd.DataFrame(rates)
        df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.set_index("timestamp").rename(
            columns={"real_volume": "volume", "tick_volume": "tick_volume"}
        )
        df["spread"] = df["spread"].astype("float64")
        return df[["open", "high", "low", "close", "tick_volume", "spread"]]

    # ----------------------------------------------------------------- orders
    def submit_market_order(self, plan: dict[str, Any]) -> Ticket:
        mt5 = _require_mt5()
        symbol = self.cfg.symbol_map.get(plan["pair"], plan["pair"].replace("_", ""))
        info = mt5.symbol_info_tick(symbol)
        if info is None:
            raise RuntimeError(f"no tick for {symbol}")
        price = info.ask if plan["direction"] > 0 else info.bid

        # MT5 deviation is in POINTS not pips. 5-digit brokers: 1 pip = 10 points.
        sym_info = mt5.symbol_info(symbol)
        digits = sym_info.digits if sym_info is not None else 5
        points_per_pip = 10 if digits == 5 else 1
        deviation_points = int(round(self.cfg.deviation_pips * points_per_pip))

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(plan["size_lots"]),
            "type": mt5.ORDER_TYPE_BUY if plan["direction"] > 0 else mt5.ORDER_TYPE_SELL,
            "price": price,
            "sl": float(plan["sl_price"]),
            "tp": float(plan["tp_price"]),
            "deviation": deviation_points,
            "magic": int(self.cfg.magic_number),
            # MT5 rejects colons / '+' / '-' and caps comment length at 31.
            # plan_id gets linked to the ticket via tickets.jsonl instead.
            "comment": "fireforex",
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        submitted_at = _now_iso()
        result = mt5.order_send(request)
        if result is None:
            raise RuntimeError(f"order_send returned None: {mt5.last_error()}")

        ticket = Ticket(
            plan_id=plan["plan_id"],
            ticket=int(getattr(result, "order", 0)),
            submitted_at=submitted_at,
            filled_at=_now_iso() if result.retcode == mt5.TRADE_RETCODE_DONE else None,
            fill_price=float(getattr(result, "price", 0.0)),
            fill_volume=float(getattr(result, "volume", 0.0)),
            retcode=int(result.retcode),
            comment=str(getattr(result, "comment", "")),
        )
        return ticket

    def modify_sl(self, ticket: int, new_sl: float) -> int:
        mt5 = _require_mt5()
        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            return -1
        pos = positions[0]
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": pos.symbol,
            "position": ticket,
            "sl": float(new_sl),
            "tp": pos.tp,
        }
        result = mt5.order_send(request)
        return int(result.retcode) if result is not None else -1

    def close_position(self, ticket: int, reason: str = "engine") -> int:
        mt5 = _require_mt5()
        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            return -1
        pos = positions[0]
        tick = mt5.symbol_info_tick(pos.symbol)
        opposite_type = (
            mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
        )
        price = tick.bid if opposite_type == mt5.ORDER_TYPE_SELL else tick.ask
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": pos.symbol,
            "position": ticket,
            "volume": pos.volume,
            "type": opposite_type,
            "price": price,
            "deviation": 20,
            "magic": int(self.cfg.magic_number),
            "comment": "fireforex-close",
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        return int(result.retcode) if result is not None else -1

    # --------------------------------------------------------------- history
    def fetch_recent_deals(self, since_ts: datetime) -> list[dict[str, Any]]:
        mt5 = _require_mt5()
        deals = mt5.history_deals_get(since_ts, datetime.now(tz=timezone.utc))
        if deals is None:
            return []
        out = []
        for d in deals:
            out.append({
                "ticket": int(d.ticket),
                "position_id": int(getattr(d, "position_id", 0)),
                "symbol": str(d.symbol),
                "type": int(d.type),
                "volume": float(d.volume),
                "price": float(d.price),
                "profit": float(d.profit),
                "time": datetime.fromtimestamp(int(d.time), tz=timezone.utc).isoformat(),
                "comment": str(getattr(d, "comment", "")),
            })
        return out

    def positions_snapshot(self) -> list[dict[str, Any]]:
        mt5 = _require_mt5()
        positions = mt5.positions_get()
        if positions is None:
            return []
        out = []
        for p in positions:
            out.append({
                "ticket": int(p.ticket),
                "symbol": str(p.symbol),
                "type": int(p.type),
                "volume": float(p.volume),
                "price_open": float(p.price_open),
                "sl": float(p.sl),
                "tp": float(p.tp),
                "time": int(p.time),
                "magic": int(p.magic),
                "comment": str(p.comment),
            })
        return out


# ── Helpers ───────────────────────────────────────────────────────────

def _now_iso() -> str:
    return pd.Timestamp.utcnow().isoformat()


def load_broker_cfg_from_env(env_file: Path | None = None) -> dict[str, Any]:
    """Load MT5 credentials from ``.env.live`` without touching `.env`.

    Returns a dict shaped for :class:`ff.live.runner.BrokerCfg`. The file is
    never read from the repo directory — it lives on the VPS only.
    """
    import os

    if env_file is not None and env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

    return {
        "login": int(os.environ["MT5_LOGIN"]),
        "password": os.environ["MT5_PASSWORD"],
        "server": os.environ["MT5_SERVER"],
        "terminal_path": os.environ.get("MT5_TERMINAL_PATH"),
    }
