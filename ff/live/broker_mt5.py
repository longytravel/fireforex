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
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

LOG = logging.getLogger(__name__)


# MT5 ``deal.reason`` code → human name.
# Sourced from the MetaTrader5 pip package: DEAL_REASON_* enum values 0..9.
# Kept local so tests and the reconciler don't need the native package.
DEAL_REASON_NAMES: dict[int, str] = {
    0: "CLIENT",  # terminal by user
    1: "MOBILE",
    2: "WEB",
    3: "EXPERT",  # EA / our own close_position → our trailing/breakeven/etc.
    4: "SL",
    5: "TP",
    6: "SO",  # stop out (margin)
    7: "ROLLOVER",
    8: "VMARGIN",
    9: "SPLIT",
}


_COMMENT_MAX_LEN = 31
_COMMENT_PREFIX = "ff_"
_COMMENT_ALIASES = {
    "ema_cross": "ema_cross",
    "macd_cross": "macd_cross",
    "donchian": "donchian",
}


def _order_comment(plan: dict[str, Any]) -> str:
    """Short MT5-safe comment showing which signal family fired."""
    raw = str(plan.get("signal_family") or "").strip().lower()
    signal = _COMMENT_ALIASES.get(raw, raw)
    signal = re.sub(r"[^a-z0-9_]+", "_", signal).strip("_")
    if not signal:
        return "fireforex"
    return f"{_COMMENT_PREFIX}{signal}"[:_COMMENT_MAX_LEN]


# Lazy import — MetaTrader5 is Windows-only and not available on dev boxes.
_mt5: Any = None


def _require_mt5() -> Any:
    global _mt5
    if _mt5 is None:
        try:
            import MetaTrader5 as mt5  # type: ignore
        except ImportError as exc:
            raise RuntimeError("MetaTrader5 pip package not installed — this is a Windows-only VPS dependency") from exc
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
        # Offset (seconds) added to MT5-returned timestamps to reach UTC.
        # IC Markets runs GMT+2/+3; MT5's `time` field is broker local
        # expressed as seconds-since-broker-epoch. Computed on connect().
        self._broker_to_utc_sec = 0

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

        # Compute broker↔UTC offset by comparing a live tick's broker-stamped
        # time against wall-clock UTC. IC Markets = GMT+2 / GMT+3 typically.
        import time as _time

        probe_symbol = self.cfg.symbol_map.get("EUR_USD", "EURUSD")
        tick = mt5.symbol_info_tick(probe_symbol)
        if tick is not None and tick.time:
            wall_utc = int(_time.time())
            # We need to SUBTRACT this from broker-stamped times to get UTC.
            self._broker_to_utc_sec = -(int(tick.time) - wall_utc)
            LOG.info("[mt5] broker-UTC offset detected: %+d s", self._broker_to_utc_sec)
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
        # MT5 position 0 is the current, still-forming M1 candle. Skip it so
        # higher-timeframe rollups only evaluate bars built from closed M1 data.
        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 1, n_bars)
        if rates is None or len(rates) == 0:
            return pd.DataFrame()
        df = pd.DataFrame(rates)
        # MT5's `time` is broker-local seconds-since-epoch, not real UTC. The
        # offset was measured in connect() and subtracted here so downstream
        # signal eval + reconciler all speak true UTC.
        df["timestamp"] = pd.to_datetime(
            df["time"].astype("int64") + int(self._broker_to_utc_sec),
            unit="s",
            utc=True,
        )
        df = df.set_index("timestamp").rename(columns={"real_volume": "volume", "tick_volume": "tick_volume"})
        df["spread"] = df["spread"].astype("float64")
        return df[["open", "high", "low", "close", "tick_volume", "spread"]]

    def current_spread_pips(self, pair: str) -> float:
        """Return the live ask−bid spread in pips, fetched at call time.

        Used by the execution guard so the 3-pip cap is checked against
        the tick at submit time rather than the closed-M1-bar mean. Falls
        back to NaN if MT5 returns no tick — the guard treats that as
        ``unknown_spread`` and fails closed.
        """
        mt5 = _require_mt5()
        symbol = self.cfg.symbol_map.get(pair, pair.replace("_", ""))
        try:
            tick = mt5.symbol_info_tick(symbol)
        except Exception:  # noqa: BLE001
            return float("nan")
        if tick is None:
            return float("nan")
        ask = float(getattr(tick, "ask", float("nan")))
        bid = float(getattr(tick, "bid", float("nan")))
        if not (ask == ask and bid == bid):  # NaN check
            return float("nan")
        pip_value = 0.01 if "JPY" in pair else 0.0001
        return (ask - bid) / pip_value

    # ----------------------------------------------------------------- orders
    def submit_market_order(self, plan: dict[str, Any]) -> Ticket:
        mt5 = _require_mt5()
        symbol = self.cfg.symbol_map.get(plan["pair"], plan["pair"].replace("_", ""))
        try:
            mt5.symbol_select(symbol, True)
        except AttributeError:
            pass
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
            # MT5 caps comments at 31 chars. Keep the plan_id in
            # tickets.jsonl and use the visible MT5 comment for signal ID.
            "comment": _order_comment(plan),
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        submitted_at = _now_iso()
        result = mt5.order_send(request)
        if result is None:
            raise RuntimeError(f"order_send returned None: {mt5.last_error()}")

        requote_code = int(getattr(mt5, "TRADE_RETCODE_REQUOTE", 10004))
        done_code = int(getattr(mt5, "TRADE_RETCODE_DONE", 10009))
        if int(result.retcode) == requote_code:
            retry = dict(request)
            retry["deviation"] = int(request["deviation"]) * 2
            tick = mt5.symbol_info_tick(symbol)
            if tick is not None:
                retry["price"] = tick.ask if plan["direction"] > 0 else tick.bid
            LOG.warning(
                "[mt5] requote on %s plan=%s; retrying deviation=%s points",
                symbol,
                plan.get("plan_id"),
                retry["deviation"],
            )
            result = mt5.order_send(retry)
            if result is None:
                raise RuntimeError(f"order_send retry returned None: {mt5.last_error()}")

        if int(result.retcode) != done_code:
            raise RuntimeError(
                "order_send rejected "
                f"plan={plan.get('plan_id')} symbol={symbol} "
                f"retcode={int(result.retcode)} "
                f"comment={getattr(result, 'comment', '')!r} "
                f"last_error={mt5.last_error()!r}"
            )

        ticket = Ticket(
            plan_id=plan["plan_id"],
            ticket=int(getattr(result, "order", 0)),
            submitted_at=submitted_at,
            filled_at=_now_iso(),
            fill_price=float(getattr(result, "price", 0.0)),
            fill_volume=float(getattr(result, "volume", 0.0)),
            retcode=int(result.retcode),
            comment=str(getattr(result, "comment", "")),
        )
        if ticket.ticket <= 0:
            raise RuntimeError(f"order_send filled but returned no ticket: {result!r}")
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

    def partial_close(self, ticket: int, pct: float) -> int:
        """Close a fraction of an open position.

        ``pct`` is a fraction (0..1) — a 50% partial passes ``0.5``. The
        close goes via a normal opposite-direction market order on
        ``volume = pos.volume * pct``. If MT5 cannot fill the rounded
        volume the call returns the raw retcode; the caller logs it and
        the reconciler surfaces the mismatch.
        """
        mt5 = _require_mt5()
        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            return -1
        pos = positions[0]
        tick = mt5.symbol_info_tick(pos.symbol)
        opposite_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
        price = tick.bid if opposite_type == mt5.ORDER_TYPE_SELL else tick.ask
        sym_info = mt5.symbol_info(pos.symbol)
        volume_step = float(getattr(sym_info, "volume_step", 0.01)) if sym_info else 0.01
        raw_volume = pos.volume * float(pct)
        # Round DOWN to the broker's volume step — over-shooting would
        # close more than intended if rounding nudges up.
        if volume_step > 0:
            close_volume = (int(raw_volume / volume_step)) * volume_step
        else:
            close_volume = raw_volume
        if close_volume <= 0:
            return -2  # sentinel: partial amount rounds to zero lots
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": pos.symbol,
            "position": ticket,
            "volume": close_volume,
            "type": opposite_type,
            "price": price,
            "deviation": 20,
            "magic": int(self.cfg.magic_number),
            "comment": "fireforex-partial",
            "type_filling": mt5.ORDER_FILLING_IOC,
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
        opposite_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
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
        # MT5 deal history uses the broker-server timestamp domain, the same
        # offset observed from live ticks. Query in broker time, then persist
        # true UTC so reconciliation does not have to guess the offset later.
        offset = timedelta(seconds=int(self._broker_to_utc_sec))
        broker_since = since_ts - offset
        broker_until = datetime.now(tz=timezone.utc) - offset
        deals = mt5.history_deals_get(broker_since, broker_until)
        if deals is None:
            return []
        out = []
        for d in deals:
            reason_code = int(getattr(d, "reason", -1))
            broker_time = int(d.time)
            out.append(
                {
                    "ticket": int(d.ticket),
                    "position_id": int(getattr(d, "position_id", 0)),
                    "magic": int(getattr(d, "magic", 0)),
                    "symbol": str(d.symbol),
                    "type": int(d.type),
                    "volume": float(d.volume),
                    "price": float(d.price),
                    "profit": float(d.profit),
                    "commission": float(getattr(d, "commission", 0.0)),
                    "swap": float(getattr(d, "swap", 0.0)),
                    "fee": float(getattr(d, "fee", 0.0)),
                    "reason_code": reason_code,
                    "reason": DEAL_REASON_NAMES.get(reason_code, "UNKNOWN"),
                    "time": datetime.fromtimestamp(
                        broker_time + int(self._broker_to_utc_sec),
                        tz=timezone.utc,
                    ).isoformat(),
                    "broker_time": datetime.fromtimestamp(
                        broker_time,
                        tz=timezone.utc,
                    ).isoformat(),
                    "comment": str(getattr(d, "comment", "")),
                }
            )
        return out

    def positions_snapshot(self) -> list[dict[str, Any]]:
        mt5 = _require_mt5()
        positions = mt5.positions_get()
        if positions is None:
            return []
        out = []
        for p in positions:
            out.append(
                {
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
                }
            )
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
