"""Fixtures for vnpy_router tests: a real MainEngine + EventEngine, two
RecordingFakeGateways (quote 'FUTU' + trade 'IB'), a routing_setting.json
redirected to a temp file, and an EVENT_CONTRACT recorder.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest

from vnpy.event import EventEngine
from vnpy.trader.constant import Exchange
from vnpy.trader.engine import MainEngine
from vnpy.trader.event import EVENT_CONTRACT
from vnpy.trader.gateway import BaseGateway
from vnpy.trader.object import (
    BarData,
    CancelRequest,
    ContractData,
    HistoryRequest,
    OrderRequest,
    Product,
    QuoteRequest,
    SubscribeRequest,
)


class RecordingFakeGateway(BaseGateway):
    """Records every routing call; pushes contracts/bars on demand."""

    default_name = "FAKE"

    def __init__(self, event_engine: EventEngine, gateway_name: str) -> None:
        super().__init__(event_engine, gateway_name)
        # BaseGateway declares `exchanges: list[Exchange] = []` as an *instance*
        # variable with a class-level default, and MainEngine.add_gateway reads
        # it off the constructed instance. Set it per-instance rather than as a
        # class attribute: a class-level mutable list is shared state (ruff
        # RUF012), and re-declaring it ClassVar would be an incompatible
        # override of the base's instance variable (mypy misc / pyright
        # reportIncompatibleVariableOverride).
        self.exchanges: list[Exchange] = [Exchange.SEHK, Exchange.SMART, Exchange.NASDAQ]
        self.subscribed: list[SubscribeRequest] = []
        self.orders: list[OrderRequest] = []
        self.cancels: list[CancelRequest] = []
        self.quotes: list[QuoteRequest] = []
        self.quote_cancels: list[CancelRequest] = []
        self.history_calls: list[HistoryRequest] = []
        self.history_result: list[BarData] = []

    def connect(self, setting: dict) -> None: ...
    def close(self) -> None: ...

    def subscribe(self, req: SubscribeRequest) -> None:
        self.subscribed.append(req)

    def send_order(self, req: OrderRequest) -> str:
        self.orders.append(req)
        order = req.create_order_data(str(len(self.orders)), self.gateway_name)
        self.on_order(order)
        return order.vt_orderid

    def cancel_order(self, req: CancelRequest) -> None:
        self.cancels.append(req)

    # BaseGateway.send_quote/cancel_quote are concrete no-ops (gateway.py:228-252),
    # so an unrouted quote would be swallowed silently rather than raise —
    # record them to make the routing observable.
    def send_quote(self, req: QuoteRequest) -> str:
        self.quotes.append(req)
        quote = req.create_quote_data(str(len(self.quotes)), self.gateway_name)
        self.on_quote(quote)
        return quote.vt_quoteid

    def cancel_quote(self, req: CancelRequest) -> None:
        self.quote_cancels.append(req)

    def query_history(self, req: HistoryRequest) -> list[BarData]:
        self.history_calls.append(req)
        return list(self.history_result)

    def query_account(self) -> None: ...
    def query_position(self) -> None: ...

    # helpers -----------------------------------------------------------
    def push_contract(self, symbol: str, exchange: Exchange, size: float,
                      pricetick: float, history_data: bool) -> None:
        self.on_contract(ContractData(
            symbol=symbol, exchange=exchange, name=symbol, product=Product.EQUITY,
            size=size, pricetick=pricetick, history_data=history_data,
            gateway_name=self.gateway_name,
        ))


@pytest.fixture
def event_engine() -> Iterator[EventEngine]:
    # Do NOT start here — MainEngine.__init__ starts it (starting twice raises
    # "threads can only be started once"). Tests that use the engine without a
    # MainEngine (e.g. the suppress-mixin test) don't need dispatch.
    ee = EventEngine()
    yield ee
    if getattr(ee, "_active", False):
        ee.stop()


@pytest.fixture
def main_engine(event_engine: EventEngine) -> Iterator[MainEngine]:
    me = MainEngine(event_engine)   # starts the event engine
    yield me
    me.close()                      # stops the event engine


@pytest.fixture
def gateways(main_engine: MainEngine) -> dict[str, RecordingFakeGateway]:
    """Register FUTU (quote) + IB (trade); return both by name."""
    futu = main_engine.add_gateway(RecordingFakeGateway, "FUTU")
    ib = main_engine.add_gateway(RecordingFakeGateway, "IB")
    return {"FUTU": futu, "IB": ib}


@pytest.fixture
def routing_config(monkeypatch, tmp_path) -> None:
    """Redirect routing_setting.json to a temp file with FUTU/IB."""
    cfg = tmp_path / "routing_setting.json"
    cfg.write_text(json.dumps({"quote_gateway": "FUTU", "trade_gateway": "IB"}), encoding="utf-8")
    monkeypatch.setattr("vnpy_router.engine.get_file_path", lambda name: cfg)


@pytest.fixture
def contract_events(event_engine: EventEngine) -> list[ContractData]:
    """Every EVENT_CONTRACT the bus sees (for backstop no-loop assertions)."""
    seen: list[ContractData] = []
    event_engine.register(EVENT_CONTRACT, lambda e: seen.append(e.data))
    return seen
