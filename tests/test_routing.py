"""Routing behaviour tests — maps to the design's BDD scenarios."""

from __future__ import annotations

import json
import time
from datetime import datetime

import pytest
from vnpy.trader.constant import Direction, Exchange, Interval, Offset, OrderType
from vnpy.trader.event import EVENT_LOG
from vnpy.trader.object import (
    BarData,
    CancelRequest,
    HistoryRequest,
    OrderRequest,
    SubscribeRequest,
)
from vnpy_gatewaykit import SuppressContractMixin, market_tz

from tests.conftest import RecordingFakeGateway
from vnpy_router import RouterConfigError, RouterEngine

# Every fixture instrument below is 700.SEHK, so its bars/history windows are
# stamped in the SEHK market's own timezone (Asia/Hong_Kong) — not the machine's
# (this project runs on US Pacific, where a naive datetime would be read ~15h
# off). Same source of truth the gateways localize feed timestamps with.
_SEHK_TZ = market_tz(Exchange.SEHK)


def _sub() -> SubscribeRequest:
    return SubscribeRequest(symbol="700", exchange=Exchange.SEHK)


def _order(price: float = 580.0, volume: float = 100) -> OrderRequest:
    return OrderRequest(symbol="700", exchange=Exchange.SEHK, direction=Direction.LONG,
                        type=OrderType.LIMIT, volume=volume, price=price, offset=Offset.OPEN)


def _add_router(main_engine) -> RouterEngine:
    return main_engine.add_engine(RouterEngine)


# --- forced routing ---------------------------------------------------

def test_subscribe_forced_to_quote(main_engine, gateways, routing_config) -> None:
    _add_router(main_engine)
    main_engine.subscribe(_sub(), "IB")   # caller asks IB — must go to FUTU
    assert len(gateways["FUTU"].subscribed) == 1
    assert gateways["FUTU"].subscribed[0].vt_symbol == "700.SEHK"
    assert gateways["IB"].subscribed == []


def test_query_history_forced_to_quote(main_engine, gateways, routing_config) -> None:
    gateways["FUTU"].history_result = [
        BarData(gateway_name="FUTU", symbol="700", exchange=Exchange.SEHK,
                datetime=datetime(2026, 7, i + 1, tzinfo=_SEHK_TZ), interval=Interval.DAILY,
                open_price=1, high_price=1, low_price=1, close_price=1, volume=1)
        for i in range(3)
    ]
    _add_router(main_engine)
    req = HistoryRequest(symbol="700", exchange=Exchange.SEHK, interval=Interval.DAILY,
                         start=datetime(2026, 7, 1, tzinfo=_SEHK_TZ))
    bars = main_engine.query_history(req, "IB")
    assert len(bars) == 3
    assert gateways["IB"].history_calls == []


def test_send_order_forced_to_trade_with_warn(
    main_engine, gateways, routing_config, event_engine
) -> None:
    logs: list = []
    event_engine.register(EVENT_LOG, lambda e: logs.append(e.data.msg))
    _add_router(main_engine)
    vt_orderid = main_engine.send_order(_order(), "FUTU")   # caller asks FUTU — must go to IB
    time.sleep(0.1)
    assert len(gateways["IB"].orders) == 1
    assert gateways["IB"].orders[0].volume == 100 and gateways["IB"].orders[0].price == 580.0
    assert gateways["FUTU"].orders == []
    assert vt_orderid.startswith("IB.")
    assert any("强制改向" in m and "IB" in m for m in logs)


def test_send_order_no_warn_when_correct(
    main_engine, gateways, routing_config, event_engine
) -> None:
    logs: list = []
    event_engine.register(EVENT_LOG, lambda e: logs.append(e.data.msg))
    _add_router(main_engine)
    main_engine.send_order(_order(), "IB")
    time.sleep(0.1)
    assert len(gateways["IB"].orders) == 1
    assert not any("强制改向" in m for m in logs)


def test_cancel_order_follows_order_gateway(main_engine, gateways, routing_config) -> None:
    _add_router(main_engine)
    # cancel_order is NOT patched — MainEngine routes by the gateway_name given.
    main_engine.cancel_order(
        CancelRequest(orderid="20260723-001", symbol="700", exchange=Exchange.SEHK), "IB"
    )
    assert len(gateways["IB"].cancels) == 1
    assert gateways["IB"].cancels[0].orderid == "20260723-001"
    assert gateways["FUTU"].cancels == []


# --- contract-collision backstop --------------------------------------

def test_contract_conflict_quote_wins(main_engine, gateways, routing_config) -> None:
    router = _add_router(main_engine)
    gateways["FUTU"].push_contract("700", Exchange.SEHK, size=100, pricetick=0.2, history_data=True)
    gateways["IB"].push_contract("700", Exchange.SEHK, size=1, pricetick=0.01, history_data=False)
    time.sleep(0.2)
    c = main_engine.get_contract("700.SEHK")
    assert c.size == 100 and c.pricetick == 0.2 and c.gateway_name == "FUTU"
    tc = router.get_trade_contract("700.SEHK")
    assert tc.size == 1 and tc.gateway_name == "IB"


def test_backstop_no_loop(main_engine, gateways, routing_config, contract_events) -> None:
    _add_router(main_engine)
    gateways["FUTU"].push_contract("700", Exchange.SEHK, size=100, pricetick=0.2, history_data=True)
    gateways["IB"].push_contract("700", Exchange.SEHK, size=1, pricetick=0.01, history_data=False)
    time.sleep(0.3)
    n = sum(1 for c in contract_events if c.vt_symbol == "700.SEHK")
    # FUTU push + IB push + one Router re-put = exactly 3, no loop.
    assert n == 3, f"expected 3 EVENT_CONTRACT, got {n}"


# --- SuppressContractMixin --------------------------------------------

def test_suppress_mixin_pushes_no_contract(event_engine, contract_events) -> None:
    class SuppressGW(SuppressContractMixin, RecordingFakeGateway):
        pass

    sink: list = []
    gw = SuppressGW(event_engine, "USMART", contract_sink=sink.append)
    for i in range(500):
        gw.push_contract(str(i), Exchange.SEHK, size=100, pricetick=0.2, history_data=True)
    time.sleep(0.2)
    assert not any(c.gateway_name == "USMART" for c in contract_events)
    assert len(sink) == 500


# --- OffsetConverter for trade gateway --------------------------------

def test_trade_gateway_has_offset_converter(main_engine, gateways, routing_config) -> None:
    _add_router(main_engine)
    assert main_engine.get_converter("IB") is not None


# --- fail-closed config -----------------------------------------------

def test_missing_config_raises(main_engine, gateways, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("vnpy_router.engine.get_file_path", lambda name: tmp_path / "nope.json")
    with pytest.raises(RouterConfigError):
        _add_router(main_engine)


def test_unregistered_gateway_raises(main_engine, gateways, monkeypatch, tmp_path) -> None:
    cfg = tmp_path / "routing_setting.json"
    cfg.write_text(json.dumps({"quote_gateway": "FUTU", "trade_gateway": "NOPE"}))
    monkeypatch.setattr("vnpy_router.engine.get_file_path", lambda name: cfg)
    with pytest.raises(RouterConfigError):
        _add_router(main_engine)


def test_same_gateway_both_roles_raises(main_engine, gateways, monkeypatch, tmp_path) -> None:
    cfg = tmp_path / "routing_setting.json"
    cfg.write_text(json.dumps({"quote_gateway": "FUTU", "trade_gateway": "FUTU"}))
    monkeypatch.setattr("vnpy_router.engine.get_file_path", lambda name: cfg)
    with pytest.raises(RouterConfigError):
        _add_router(main_engine)


def test_double_patch_raises(main_engine, gateways, routing_config) -> None:
    _add_router(main_engine)
    with pytest.raises(RouterConfigError):
        _add_router(main_engine)


# --- LIVE hard-assert -------------------------------------------------

class _StubEngine:
    def close(self) -> None: ...


def test_live_rejects_paper_account(main_engine, gateways, routing_config) -> None:
    router = _add_router(main_engine)
    # Simulate PaperAccountApp being loaded.
    main_engine.engines["PaperAccount"] = _StubEngine()
    with pytest.raises(RuntimeError) as exc:
        router.verify_patch_chain("LIVE")
    assert "PaperAccount" in str(exc.value)


def test_paper_profile_allows_paper_account(main_engine, gateways, routing_config) -> None:
    router = _add_router(main_engine)
    main_engine.engines["PaperAccount"] = _StubEngine()
    router.verify_patch_chain("PAPER")   # must NOT raise
