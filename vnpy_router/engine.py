"""RouterEngine — split quote/trade gateway routing.

Forces every market-data call (subscribe / query_history) to the quote
gateway and every order to the trade gateway, regardless of the gateway_name
the caller passed, by monkey-patching MainEngine's four routing methods (the
same mechanism vnpy_paperaccount / vnpy_riskmanager already use). Also fixes
the OMS contract-table collision (OmsEngine keys contracts by vt_symbol
alone, so a trade gateway pushing a symbol overwrites the quote gateway's
real size/pricetick) via an EVENT_CONTRACT backstop: the quote gateway stays
the OMS owner, the trade side is cached privately for order validation.

See docs/plans/2026-07-23-split-quote-trade-routing-design/ for the full
rationale, requirements (REQ-001..013) and BDD scenarios.

Fail-closed: a missing/malformed routing_setting.json, an unregistered
gateway name, or a double-patch raises at construction — the router never
installs a partial/ambiguous patch that could route a live order to the
wrong account.
"""

from __future__ import annotations

import json
import threading

from vnpy.event import Event, EventEngine
from vnpy.trader.converter import OffsetConverter
from vnpy.trader.engine import BaseEngine, MainEngine, OmsEngine
from vnpy.trader.event import EVENT_CONTRACT
from vnpy.trader.object import (
    ContractData,
    HistoryRequest,
    OrderRequest,
    SubscribeRequest,
)
from vnpy.trader.utility import get_file_path

APP_NAME = "router"
SETTING_FILENAME = "routing_setting.json"
PAPER_ENGINE_NAME = "PaperAccount"


class RouterConfigError(RuntimeError):
    """Raised when routing config is missing/invalid — fail-closed, no patch."""


class RouterEngine(BaseEngine):
    """Centralized split quote/trade routing. Added via
    main_engine.add_engine(RouterEngine) AFTER gateways are registered and
    BEFORE PaperAccountApp/RiskManagerApp are loaded."""

    def __init__(self, main_engine: MainEngine, event_engine: EventEngine) -> None:
        super().__init__(main_engine, event_engine, APP_NAME)

        setting: dict = self._load_setting()
        self.quote_gateway: str = setting["quote_gateway"]
        self.trade_gateway: str = setting["trade_gateway"]

        self._assert_not_double_patched()       # REQ-013
        self._validate_gateways_registered()    # REQ-012

        # Quote gateway owns OMS contracts; trade-side kept private for
        # order validation (REQ-005). _lock guards the multi-key updates;
        # _reasserting breaks the backstop re-put loop (REQ-011).
        self._quote_contracts: dict[str, ContractData] = {}
        self.trade_contracts: dict[str, ContractData] = {}
        self._lock = threading.Lock()
        self._reasserting: set[str] = set()

        # Save the original bound methods (the next link in the patch chain).
        self._subscribe = main_engine.subscribe
        self._query_history = main_engine.query_history
        self._send_order = main_engine.send_order

        main_engine.subscribe = self.subscribe            # type: ignore[method-assign]
        main_engine.query_history = self.query_history    # type: ignore[method-assign]
        main_engine.send_order = self.send_order          # type: ignore[method-assign]
        # cancel_order is deliberately NOT patched — a cancel must go to the
        # gateway that owns the order (vt_orderid prefix is authoritative);
        # forcing it would misroute cancels. MainEngine's original routing by
        # order.gateway_name is already correct (REQ-004).

        self._patched = True   # marker for _assert_not_double_patched

        self._ensure_trade_converter()   # REQ-007

        # Backstop registered after OmsEngine (OMS registers in its own
        # __init__, RouterEngine is added later, so this handler runs after
        # OMS has stored the contract and can reclaim ownership).
        event_engine.register(EVENT_CONTRACT, self._on_contract)

    # ------------------------------------------------------------------
    # Config (fail-closed)
    # ------------------------------------------------------------------
    def _load_setting(self) -> dict:
        path = get_file_path(SETTING_FILENAME)
        if not path.exists():
            raise RouterConfigError(
                f"路由配置缺失: {path} —— 需含 {{'quote_gateway': ..., 'trade_gateway': ...}}"
                f"(fail-closed: 未配置则不安装任何路由 patch)"
            )
        try:
            setting = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise RouterConfigError(f"路由配置解析失败 {path}: {exc}") from exc

        for key in ("quote_gateway", "trade_gateway"):
            if not setting.get(key):
                raise RouterConfigError(f"路由配置缺少 '{key}': {setting}")
        if setting["quote_gateway"] == setting["trade_gateway"]:
            raise RouterConfigError(
                f"quote_gateway 与 trade_gateway 不能相同: {setting['quote_gateway']}"
            )
        return setting

    def _validate_gateways_registered(self) -> None:
        names = self.main_engine.get_all_gateway_names()
        for role, name in (("quote_gateway", self.quote_gateway), ("trade_gateway", self.trade_gateway)):
            if name not in names:
                raise RouterConfigError(
                    f"{role} '{name}' 未注册(已注册: {names}) —— fail-closed,不安装 patch"
                )

    def _assert_not_double_patched(self) -> None:
        for attr in ("subscribe", "send_order", "query_history"):
            method = getattr(self.main_engine, attr)
            owner = getattr(method, "__self__", None)
            if isinstance(owner, RouterEngine):
                raise RouterConfigError(
                    f"MainEngine.{attr} 已被另一个 RouterEngine patch —— 禁止重复添加"
                )

    def _ensure_trade_converter(self) -> None:
        # A trade gateway using SuppressContractMixin pushes no contracts, so
        # OmsEngine never builds its OffsetConverter — create it explicitly so
        # convert_order_request works for trade-gateway orders.
        # engines[] is typed BaseEngine, so both the attribute access and
        # OffsetConverter's parameter need the concrete OmsEngine. Assert rather
        # than cast: if vnpy ever stops registering an OmsEngine under "oms",
        # silently proceeding would leave orders unconvertible at trade time.
        oms = self.main_engine.engines["oms"]
        if not isinstance(oms, OmsEngine):
            raise RouterConfigError(f"MainEngine 的 'oms' 引擎不是 OmsEngine: {type(oms).__name__}")
        oms.offset_converters.setdefault(self.trade_gateway, OffsetConverter(oms))

    # ------------------------------------------------------------------
    # Forced routing
    # ------------------------------------------------------------------
    def subscribe(self, req: SubscribeRequest, gateway_name: str) -> None:
        if self.main_engine.get_contract(req.vt_symbol) is None:
            self.main_engine.write_log(
                f"行情网关 {self.quote_gateway} 无合约 {req.vt_symbol},仍投递给它(不回退交易网关)",
                APP_NAME,
            )
        # Force the quote gateway regardless of the caller's gateway_name; do
        # NOT silently fall back to the trade gateway's (possibly delayed) feed.
        self._subscribe(req, self.quote_gateway)

    def query_history(self, req: HistoryRequest, gateway_name: str) -> list:
        return self._query_history(req, self.quote_gateway)

    def send_order(self, req: OrderRequest, gateway_name: str) -> str:
        if gateway_name != self.trade_gateway:
            self.main_engine.write_log(
                f"send_order 请求发往 {gateway_name},已强制改向交易网关 {self.trade_gateway}",
                APP_NAME,
            )
        return self._send_order(req, self.trade_gateway)

    # ------------------------------------------------------------------
    # Contract-collision backstop (REQ-005/006/011)
    # ------------------------------------------------------------------
    def _on_contract(self, event: Event) -> None:
        contract: ContractData = event.data

        if contract.gateway_name == self.quote_gateway:
            # Quote side is authoritative; remember it, never re-put (would loop).
            self._quote_contracts[contract.vt_symbol] = contract
            self._reasserting.discard(contract.vt_symbol)
            return

        if contract.gateway_name == self.trade_gateway:
            with self._lock:
                self.trade_contracts[contract.vt_symbol] = contract
            # If the quote side also has this symbol, the trade contract just
            # overwrote the OMS entry — re-put the quote contract to reclaim
            # ownership. The re-put re-enters this handler with
            # gateway_name == quote_gateway → the branch above returns without
            # re-putting, so there is no loop; _reasserting is belt-and-braces.
            quote_c = self._quote_contracts.get(contract.vt_symbol)
            if quote_c is not None and contract.vt_symbol not in self._reasserting:
                self._reasserting.add(contract.vt_symbol)
                self.event_engine.put(Event(EVENT_CONTRACT, quote_c))

    def add_trade_contract(self, contract: ContractData) -> None:
        """contract_sink for SuppressContractMixin trade gateways: they push
        no EVENT_CONTRACT, so feed their contracts straight into the trade-side
        cache for order validation."""
        with self._lock:
            self.trade_contracts[contract.vt_symbol] = contract

    def get_trade_contract(self, vt_symbol: str) -> ContractData | None:
        return self.trade_contracts.get(vt_symbol)

    # ------------------------------------------------------------------
    # Startup audit (REQ-010)
    # ------------------------------------------------------------------
    def verify_patch_chain(self, profile: str) -> None:
        """Called from run_gui AFTER apps are added. In LIVE, PaperAccountApp
        must NOT be loaded — its send_order hijack is unconditional and would
        silently swallow real orders. Fail-closed: raise before any gateway
        connects."""
        if profile.upper() == "LIVE" and PAPER_ENGINE_NAME in self.main_engine.engines:
            raise RuntimeError(
                "LIVE 档拒绝启动: PaperAccountApp 已加载 —— 它会无条件劫持 send_order "
                "并吞掉真实订单。LIVE 档禁止加载 PaperAccountApp。"
            )
