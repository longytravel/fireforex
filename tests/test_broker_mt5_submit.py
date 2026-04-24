from __future__ import annotations

import pandas as pd
from types import SimpleNamespace

import pytest

from ff.live import broker_mt5
from ff.live.runner import BrokerCfg


class _FakeMT5:
    TIMEFRAME_M1 = 1
    TRADE_ACTION_DEAL = 1
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_FILLING_IOC = 1
    TRADE_RETCODE_DONE = 10009
    TRADE_RETCODE_REQUOTE = 10004

    def __init__(self, results):
        self.results = list(results)
        self.requests = []

    def symbol_select(self, _symbol, _enabled):
        return True

    def symbol_info_tick(self, _symbol):
        return SimpleNamespace(ask=1.1002, bid=1.1000)

    def symbol_info(self, _symbol):
        return SimpleNamespace(digits=5)

    def order_send(self, request):
        self.requests.append(dict(request))
        return self.results.pop(0)

    def last_error(self):
        return (0, "ok")


class _FakeMT5Rates:
    TIMEFRAME_M1 = 1

    def __init__(self):
        self.calls = []

    def copy_rates_from_pos(self, symbol, timeframe, start_pos, count):
        self.calls.append((symbol, timeframe, start_pos, count))
        return [
            {
                "time": 1_775_000_000,
                "open": 1.1,
                "high": 1.2,
                "low": 1.0,
                "close": 1.15,
                "tick_volume": 42,
                "spread": 3,
                "real_volume": 0,
            }
        ]


def _plan():
    return {
        "plan_id": "inst_EUR_USD_2026-04-24T10:00:00+00:00_+1",
        "pair": "EUR_USD",
        "direction": 1,
        "size_lots": 0.01,
        "sl_price": 1.0900,
        "tp_price": 1.1200,
        "signal_family": "ema_cross",
    }


def test_submit_market_order_raises_on_reject(monkeypatch):
    fake = _FakeMT5([
        SimpleNamespace(retcode=10016, order=0, price=0.0, volume=0.0,
                        comment="invalid stops"),
    ])
    monkeypatch.setattr(broker_mt5, "_mt5", fake)

    broker = broker_mt5.MT5Broker(
        BrokerCfg(login=1, password="x", server="x", deviation_pips=3)
    )
    with pytest.raises(RuntimeError, match="order_send rejected"):
        broker.submit_market_order(_plan())


def test_submit_market_order_retries_requote_once(monkeypatch):
    fake = _FakeMT5([
        SimpleNamespace(retcode=10004, order=0, price=0.0, volume=0.0,
                        comment="requote"),
        SimpleNamespace(retcode=10009, order=12345, price=1.1002,
                        volume=0.01, comment="done"),
    ])
    monkeypatch.setattr(broker_mt5, "_mt5", fake)

    broker = broker_mt5.MT5Broker(
        BrokerCfg(login=1, password="x", server="x", deviation_pips=3)
    )
    ticket = broker.submit_market_order(_plan())

    assert ticket.ticket == 12345
    assert len(fake.requests) == 2
    assert fake.requests[0]["deviation"] == 30
    assert fake.requests[1]["deviation"] == 60


def test_submit_market_order_comment_names_signal(monkeypatch):
    fake = _FakeMT5([
        SimpleNamespace(retcode=10009, order=12345, price=1.1002,
                        volume=0.01, comment="done"),
    ])
    monkeypatch.setattr(broker_mt5, "_mt5", fake)

    broker = broker_mt5.MT5Broker(
        BrokerCfg(login=1, password="x", server="x", deviation_pips=3)
    )
    broker.submit_market_order(_plan())

    assert fake.requests[0]["comment"] == "ff_ema_cross"


def test_copy_rates_m1_skips_current_forming_bar(monkeypatch):
    fake = _FakeMT5Rates()
    monkeypatch.setattr(broker_mt5, "_mt5", fake)

    broker = broker_mt5.MT5Broker(
        BrokerCfg(
            login=1,
            password="x",
            server="x",
            deviation_pips=3,
            symbol_map={"EUR_USD": "EURUSD.a"},
        )
    )
    df = broker.copy_rates_m1("EUR_USD", 200)

    assert fake.calls == [("EURUSD.a", fake.TIMEFRAME_M1, 1, 200)]
    assert isinstance(df.index, pd.DatetimeIndex)
    assert df.index.tz is not None
    assert df.iloc[0]["close"] == 1.15
