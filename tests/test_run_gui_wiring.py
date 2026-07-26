"""The audit against the real app stack, in run_gui.py's load order.

test_patch_guards.py proves verify_patch_chain rejects a bypassed chain using
PaperEngine/RiskEngine-shaped stubs. A false *positive* is the more expensive
failure — it refuses to start the terminal — and stubs cannot prove its
absence, because the audit's ability to follow a link depends on how the real
patchers store the method they captured. So this file wires the actual
vnpy_paperaccount / vnpy_riskmanager engines exactly the way
vnpy_app/run_gui.py does and asserts the audit passes.
"""

from __future__ import annotations

import pytest
from vnpy.trader.engine import MainEngine
from vnpy_paperaccount import PaperAccountApp
from vnpy_riskmanager import RiskManagerApp

from vnpy_router import RouterEngine


@pytest.fixture(autouse=True)
def _no_json_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    """PaperEngine/RiskEngine persist into the user's real ~/.vntrader. Reads
    are harmless (missing file -> {}), writes are not: a timer tick during the
    test would overwrite the user's own paper positions with this test's empty
    ones."""
    monkeypatch.setattr("vnpy_paperaccount.engine.save_json", lambda *a, **k: None)
    monkeypatch.setattr("vnpy_riskmanager.engine.save_json", lambda *a, **k: None)


def test_paper_profile_wiring_passes_the_audit(main_engine: MainEngine, gateways,
                                               routing_config) -> None:
    """run_gui PAPER: Router -> PaperAccountApp -> RiskManagerApp.

    send_order ends at PaperEngine (it never forwards) — audited leniently;
    subscribe/query_history must still reach the router, and do, because
    PaperEngine keeps them as self._subscribe / self._query_history.
    """
    router = main_engine.add_engine(RouterEngine)
    assert isinstance(router, RouterEngine)
    main_engine.add_app(PaperAccountApp)
    main_engine.add_app(RiskManagerApp)

    router.verify_patch_chain("PAPER")     # must NOT raise

    assert any(owner is router for owner in router.patch_chain("subscribe"))
    assert any(owner is router for owner in router.patch_chain("query_history"))
    # The order path stops at PaperEngine: that is what PAPER means.
    assert not any(owner is router for owner in router.patch_chain("send_order"))


def test_live_profile_wiring_passes_the_audit(main_engine: MainEngine, gateways,
                                              routing_config) -> None:
    """run_gui LIVE: Router -> RiskManagerApp, no PaperAccountApp.

    RiskEngine wraps send_order and keeps the previous bound method as
    self._send_order, so the router stays provably in the order chain.
    """
    router = main_engine.add_engine(RouterEngine)
    assert isinstance(router, RouterEngine)
    main_engine.add_app(RiskManagerApp)

    router.verify_patch_chain("LIVE")      # must NOT raise

    chain = router.patch_chain("send_order")
    assert any(owner is router for owner in chain)
    assert chain[-1] is main_engine        # reaches MainEngine's own dispatch
