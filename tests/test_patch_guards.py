"""Patch-chain integrity tests — the fail-closed premise itself.

The module docstring of vnpy_router.engine promises three things about
construction: a missing/malformed config, an unregistered gateway name, or a
double patch all raise, and "the router never installs a partial/ambiguous
patch that could route a live order to the wrong account". test_routing.py
covers the happy paths of those guards; this file covers the ways the guards
are evaded — a later patcher shadowing the double-patch probe, a construction
that raises after the patches are already installed, a chain audit that never
looks at the chain, and the quote (two-sided order) path that no patch covers.
"""

from __future__ import annotations

import pytest
from vnpy.trader.constant import Exchange
from vnpy.trader.engine import MainEngine
from vnpy.trader.object import CancelRequest, QuoteRequest

from vnpy_router import RouterConfigError, RouterEngine


def _add_router(main_engine: MainEngine) -> RouterEngine:
    return main_engine.add_engine(RouterEngine)


def _quote() -> QuoteRequest:
    return QuoteRequest(symbol="700", exchange=Exchange.SEHK, bid_price=579.0,
                        bid_volume=100, ask_price=581.0, ask_volume=100)


class _ReplacingEngine:
    """A PaperEngine-shaped patcher: replaces send_order and never chains.

    vnpy_paperaccount.engine.PaperEngine.__init__ does exactly this for
    send_order / cancel_order / send_quote / cancel_quote (it only keeps the
    previous bound method for subscribe / query_history).
    """

    ALL = ("subscribe", "query_history", "send_order", "send_quote")

    def __init__(self, main_engine: MainEngine, methods: tuple[str, ...] = ALL) -> None:
        self.main_engine = main_engine
        self.orders: list = []
        for name in methods:
            setattr(main_engine, name, getattr(self, name))

    def subscribe(self, req, gateway_name: str) -> None: ...
    def query_history(self, req, gateway_name: str) -> list:
        return []

    def send_order(self, req, gateway_name: str) -> str:
        self.orders.append((req, gateway_name))
        return "STUB.1"

    def send_quote(self, req, gateway_name: str) -> str:
        self.orders.append((req, gateway_name))
        return "STUB.Q1"

    def close(self) -> None: ...


class _WrappingEngine:
    """A RiskEngine-shaped patcher: keeps the previous bound method and calls it
    (vnpy_riskmanager.engine.RiskEngine.patch_functions)."""

    def __init__(self, main_engine: MainEngine) -> None:
        self.main_engine = main_engine
        self._send_order = main_engine.send_order
        main_engine.send_order = self.send_order          # type: ignore[method-assign]

    def send_order(self, req, gateway_name: str) -> str:
        return self._send_order(req, gateway_name)

    def close(self) -> None: ...


class _NotAnOms:
    """Stands in for MainEngine.engines['oms'] so _ensure_trade_converter fails."""

    def close(self) -> None: ...


# --- double patch under a shadowing patcher ---------------------------

def test_double_patch_detected_even_when_shadowed(main_engine, gateways, routing_config) -> None:
    """REQ-013 must survive a later patcher.

    _assert_not_double_patched only inspects main_engine.send_order.__self__,
    so any patcher applied after the router (PaperAccountApp in the shipped
    run_gui load order) hides the router from the probe and a second
    RouterEngine installs silently — two live backstops on EVENT_CONTRACT and
    a patch chain nobody can audit.
    """
    _add_router(main_engine)
    _ReplacingEngine(main_engine)          # shadows the router's patches

    with pytest.raises(RouterConfigError):
        _add_router(main_engine)


def test_double_patch_detected_on_manual_construction(main_engine, gateways, routing_config,
                                                      event_engine) -> None:
    """add_engine keys by engine_name, so a manual construction bypasses that
    dedup entirely — the guard must not rely on it."""
    _add_router(main_engine)
    _ReplacingEngine(main_engine)

    with pytest.raises(RouterConfigError):
        RouterEngine(main_engine, event_engine)


# --- partial patch on a failed construction ---------------------------

def test_failed_construction_leaves_main_engine_unpatched(main_engine, gateways,
                                                          routing_config) -> None:
    """"never installs a partial patch" — everything that can raise must raise
    before main_engine is mutated.

    _ensure_trade_converter runs after the three assignments, so a MainEngine
    without a real OmsEngine ends up with the routing patches installed by a
    RouterEngine whose construction failed: nothing owns them, nothing can
    audit them, and the caller believes no patch was applied.
    """
    original_send_order = main_engine.send_order
    original_subscribe = main_engine.subscribe
    original_query_history = main_engine.query_history
    main_engine.engines["oms"] = _NotAnOms()   # type: ignore[assignment]

    with pytest.raises(RouterConfigError):
        _add_router(main_engine)

    assert main_engine.send_order == original_send_order
    assert main_engine.subscribe == original_subscribe
    assert main_engine.query_history == original_query_history


# --- verify_patch_chain actually verifying the chain ------------------

def test_live_rejects_router_bypassed_in_send_order_chain(main_engine, gateways,
                                                          routing_config) -> None:
    """best-practices.md: the audit walks main_engine.send_order along __self__
    and compares it with the profile's expected chain.

    Today it only looks for a PaperAccount key in main_engine.engines, so any
    other non-chaining patcher loaded after the router passes the audit while
    every LIVE order goes to the caller's gateway_name — i.e. to the quote
    gateway for anything that reads contract.gateway_name.
    """
    router = _add_router(main_engine)
    _ReplacingEngine(main_engine, methods=("send_order",))   # router bypassed

    with pytest.raises(RuntimeError) as exc:
        router.verify_patch_chain("LIVE")
    assert "send_order" in str(exc.value)


def test_live_accepts_intact_wrapped_chain(main_engine, gateways, routing_config) -> None:
    """A RiskEngine-shaped wrapper keeps the router in the chain — must pass."""
    router = _add_router(main_engine)
    _WrappingEngine(main_engine)
    router.verify_patch_chain("LIVE")      # must NOT raise


def test_live_rejects_bypassed_subscribe_chain(main_engine, gateways, routing_config) -> None:
    """Market data must also be provably routed: a replaced subscribe means the
    strategy silently reads the trade gateway's (delayed) feed."""
    router = _add_router(main_engine)
    _ReplacingEngine(main_engine, methods=("subscribe",))

    with pytest.raises(RuntimeError) as exc:
        router.verify_patch_chain("LIVE")
    assert "subscribe" in str(exc.value)


def test_live_accepts_untouched_chain(main_engine, gateways, routing_config) -> None:
    """No later patcher at all — the router is the head of every chain."""
    router = _add_router(main_engine)
    router.verify_patch_chain("LIVE")      # must NOT raise


def test_paper_also_rejects_bypassed_subscribe(main_engine, gateways, routing_config) -> None:
    """Market data is audited in every profile: PaperEngine chains subscribe
    (paperaccount/engine.py:67), so a subscribe that no longer reaches the
    router means the strategy is reading someone else's feed even on paper."""
    router = _add_router(main_engine)
    _ReplacingEngine(main_engine, methods=("subscribe",))

    with pytest.raises(RuntimeError):
        router.verify_patch_chain("PAPER")


def test_paper_tolerates_paper_engine_owning_the_order_path(main_engine, gateways,
                                                            routing_config) -> None:
    """PAPER's order chain legitimately stops at PaperEngine — auditing the
    order path there would refuse to start a correctly wired paper session."""
    router = _add_router(main_engine)
    _ReplacingEngine(main_engine, methods=("send_order", "send_quote"))
    main_engine.engines["PaperAccount"] = _ReplacingEngine.__new__(_ReplacingEngine)

    router.verify_patch_chain("PAPER")     # must NOT raise


# --- quote (two-sided order) path -------------------------------------

def test_send_quote_forced_to_trade_gateway(main_engine, gateways, routing_config) -> None:
    """A QuoteRequest is an order — MainEngine.send_quote hands it straight to
    the named gateway (engine.py:276-284). Unrouted, a caller that follows
    contract.gateway_name sends a two-sided quote to the quote gateway."""
    _add_router(main_engine)
    main_engine.send_quote(_quote(), "FUTU")

    assert len(gateways["IB"].quotes) == 1
    assert gateways["IB"].quotes[0].vt_symbol == "700.SEHK"
    assert gateways["FUTU"].quotes == []


def test_cancel_quote_follows_quote_gateway_name(main_engine, gateways, routing_config) -> None:
    """Symmetric with cancel_order (REQ-004): a cancel must reach the gateway
    that owns the quote, so cancel_quote stays unpatched."""
    _add_router(main_engine)
    main_engine.cancel_quote(
        CancelRequest(orderid="20260726-001", symbol="700", exchange=Exchange.SEHK), "IB")

    assert len(gateways["IB"].quote_cancels) == 1
    assert gateways["FUTU"].quote_cancels == []
