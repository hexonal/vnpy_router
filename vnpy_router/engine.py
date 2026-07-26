"""RouterEngine — split quote/trade gateway routing.

Forces every market-data call (subscribe / query_history) to the quote
gateway and every order — single-sided (send_order) or two-sided
(send_quote) — to the trade gateway, regardless of the gateway_name the
caller passed, by monkey-patching those four MainEngine methods (the same
mechanism vnpy_paperaccount / vnpy_riskmanager already use). Also fixes
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
    BarData,
    ContractData,
    HistoryRequest,
    OrderRequest,
    QuoteRequest,
    SubscribeRequest,
)
from vnpy.trader.utility import get_file_path

APP_NAME = "router"
SETTING_FILENAME = "routing_setting.json"
PAPER_ENGINE_NAME = "PaperAccount"

# Marker left on the MainEngine instance once the patches are installed.
# The design (best-practices.md 幂等/重复 patch) calls for an explicit marker
# rather than only probing main_engine.send_order.__self__: any patcher applied
# after the router (PaperAccountApp is loaded after it in run_gui) shadows that
# probe, and MainEngine.add_engine's engine_name keying does not dedup at all —
# it constructs the engine first and only then stores it.
PATCH_MARKER = "_vnpy_router_patched"

# Market data must reach the quote gateway in every profile; orders may
# legitimately stop earlier than the router in PAPER (PaperEngine replaces
# send_order/send_quote and never forwards), so they are audited LIVE-only.
QUOTE_PATH_METHODS = ("subscribe", "query_history")
TRADE_PATH_METHODS = ("send_order", "send_quote")


class RouterConfigError(RuntimeError):
    """Raised when routing config is missing/invalid — fail-closed, no patch."""


class RouterEngine(BaseEngine):
    """Centralized split quote/trade routing. Added via
    main_engine.add_engine(RouterEngine) AFTER gateways are registered and
    BEFORE PaperAccountApp/RiskManagerApp are loaded."""

    def __init__(self, main_engine: MainEngine, event_engine: EventEngine) -> None:
        super().__init__(main_engine, event_engine, APP_NAME)

        setting: dict[str, str] = self._load_setting()
        self.quote_gateway: str = setting["quote_gateway"]
        self.trade_gateway: str = setting["trade_gateway"]

        # Everything that can raise runs BEFORE main_engine is touched: a
        # construction that fails halfway would otherwise leave the routing
        # patches installed on a MainEngine while the caller believes no patch
        # was applied — a partial patch nobody owns and nothing can audit.
        self._assert_not_double_patched()       # REQ-013
        self._validate_gateways_registered()    # REQ-012
        oms = self._resolve_oms()               # REQ-007 (validate before patching)

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
        self._send_quote = main_engine.send_quote

        main_engine.subscribe = self.subscribe            # type: ignore[method-assign]
        main_engine.query_history = self.query_history    # type: ignore[method-assign]
        main_engine.send_order = self.send_order          # type: ignore[method-assign]
        main_engine.send_quote = self.send_quote          # type: ignore[method-assign]
        # cancel_order / cancel_quote are deliberately NOT patched — a cancel
        # must go to the gateway that owns the order/quote (the vt_orderid /
        # vt_quoteid prefix is authoritative); forcing it would misroute
        # cancels. MainEngine's original routing by the caller-supplied
        # gateway_name (which every cancel path derives from the order/quote
        # itself) is already correct (REQ-004).

        setattr(main_engine, PATCH_MARKER, self)

        # A trade gateway using SuppressContractMixin pushes no contracts, so
        # OmsEngine never builds its OffsetConverter — create it explicitly so
        # convert_order_request works for trade-gateway orders (REQ-007).
        oms.offset_converters.setdefault(self.trade_gateway, OffsetConverter(oms))

        # Backstop registered after OmsEngine (OMS registers in its own
        # __init__, RouterEngine is added later, so this handler runs after
        # OMS has stored the contract and can reclaim ownership).
        event_engine.register(EVENT_CONTRACT, self._on_contract)

    # ------------------------------------------------------------------
    # Config (fail-closed)
    # ------------------------------------------------------------------
    def _load_setting(self) -> dict[str, str]:
        path = get_file_path(SETTING_FILENAME)
        if not path.exists():
            raise RouterConfigError(
                f"路由配置缺失: {path} —— 需含 {{'quote_gateway': ..., 'trade_gateway': ...}}"
                f"(fail-closed: 未配置则不安装任何路由 patch)"
            )
        try:
            loaded: object = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise RouterConfigError(f"路由配置解析失败 {path}: {exc}") from exc

        # A well-formed JSON document that is not an object would otherwise die
        # on .get() with an AttributeError — same fail-closed outcome, wrong
        # exception type for a caller that catches RouterConfigError.
        if not isinstance(loaded, dict):
            raise RouterConfigError(
                f"路由配置 {path} 顶层应是对象,实得 {type(loaded).__name__}"
            )

        setting: dict[str, str] = {}
        for key in ("quote_gateway", "trade_gateway"):
            value = loaded.get(key)
            if not value or not isinstance(value, str):
                raise RouterConfigError(f"路由配置缺少 '{key}'(或不是网关名字符串): {loaded}")
            setting[key] = value
        if setting["quote_gateway"] == setting["trade_gateway"]:
            raise RouterConfigError(
                f"quote_gateway 与 trade_gateway 不能相同: {setting['quote_gateway']}"
            )
        return setting

    def _validate_gateways_registered(self) -> None:
        names = self.main_engine.get_all_gateway_names()
        roles = (("quote_gateway", self.quote_gateway), ("trade_gateway", self.trade_gateway))
        for role, name in roles:
            if name not in names:
                raise RouterConfigError(
                    f"{role} '{name}' 未注册(已注册: {names}) —— fail-closed,不安装 patch"
                )

    def _assert_not_double_patched(self) -> None:
        # The marker is authoritative: it survives any later patcher, whereas
        # the __self__ probe below only sees the outermost patch and a
        # PaperEngine/RiskEngine layered on top of the router hides it.
        installed = getattr(self.main_engine, PATCH_MARKER, None)
        if installed is not None:
            raise RouterConfigError(
                f"MainEngine 已被 RouterEngine 打过路由 patch({type(installed).__name__} "
                f"id={id(installed):#x}) —— 禁止重复添加(第二个 Router 会把第一个的 "
                f"bound method 当成'原始实现'存下,patch 链就再也审计不了)"
            )
        # Kept as a second line of defence: a RouterEngine constructed against a
        # MainEngine whose marker was cleared by hand is still visible here.
        for attr in QUOTE_PATH_METHODS + TRADE_PATH_METHODS:
            method = getattr(self.main_engine, attr)
            owner = getattr(method, "__self__", None)
            if isinstance(owner, RouterEngine):
                raise RouterConfigError(
                    f"MainEngine.{attr} 已被另一个 RouterEngine patch —— 禁止重复添加"
                )

    def _resolve_oms(self) -> OmsEngine:
        # engines[] is typed BaseEngine, so both the attribute access and
        # OffsetConverter's parameter need the concrete OmsEngine. Assert rather
        # than cast: if vnpy ever stops registering an OmsEngine under "oms",
        # silently proceeding would leave orders unconvertible at trade time.
        oms = self.main_engine.engines.get("oms")
        if not isinstance(oms, OmsEngine):
            raise RouterConfigError(
                f"MainEngine 的 'oms' 引擎不是 OmsEngine: {type(oms).__name__}"
            )
        return oms

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

    def query_history(self, req: HistoryRequest, gateway_name: str) -> list[BarData]:
        return self._query_history(req, self.quote_gateway)

    def send_order(self, req: OrderRequest, gateway_name: str) -> str:
        if gateway_name != self.trade_gateway:
            self.main_engine.write_log(
                f"send_order 请求发往 {gateway_name},已强制改向交易网关 {self.trade_gateway}",
                APP_NAME,
            )
        return self._send_order(req, self.trade_gateway)

    def send_quote(self, req: QuoteRequest, gateway_name: str) -> str:
        # A QuoteRequest is a two-sided order, not market data: MainEngine
        # hands it straight to the named gateway, and BaseGateway's default
        # send_quote is a silent no-op returning "" — so an unrouted quote
        # either reaches the wrong account or vanishes without an error.
        # Same forced routing + audit log as send_order (REQ-003).
        if gateway_name != self.trade_gateway:
            self.main_engine.write_log(
                f"send_quote 请求发往 {gateway_name},已强制改向交易网关 {self.trade_gateway}",
                APP_NAME,
            )
        return self._send_quote(req, self.trade_gateway)

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
    def _next_link(self, owner: object, attr: str) -> object | None:
        """The patch a wrapper captured before installing its own.

        Every in-tree patcher keeps it as an instance attribute — RiskEngine as
        ``self._send_order`` (riskmanager/engine.py:129), PaperEngine as
        ``self._subscribe``/``self._query_history``
        (paperaccount/engine.py:67-68), this engine likewise. Preferring the
        ``_<attr>`` name keeps the walk deterministic when an object holds
        several captured methods; the value scan then catches the patcher that
        named its attribute differently.
        """
        try:
            attributes: dict[str, object] = vars(owner)
        except TypeError:          # __slots__ / builtin — nothing to follow
            return None

        def is_link(value: object) -> bool:
            return (
                callable(value)
                and getattr(value, "__name__", None) == attr
                and getattr(value, "__self__", None) is not None
                and getattr(value, "__self__", None) is not owner
            )

        for name in (f"_{attr}", f"_original_{attr}", f"_prev_{attr}"):
            candidate = attributes.get(name)
            if is_link(candidate):
                return candidate
        for candidate in attributes.values():
            if is_link(candidate):
                return candidate
        return None

    def patch_chain(self, attr: str) -> list[object]:
        """Owners of ``main_engine.<attr>``, outermost first.

        Ends at the MainEngine itself when the chain is intact (its own bound
        method is the innermost link), or at the last patcher whose captured
        predecessor could not be found — that truncation is exactly what the
        audit below treats as "cannot prove this call reaches the router".
        """
        chain: list[object] = []
        seen: set[int] = set()
        link: object | None = getattr(self.main_engine, attr, None)
        while callable(link):
            owner = getattr(link, "__self__", None)
            if owner is None or id(owner) in seen:
                break
            chain.append(owner)
            seen.add(id(owner))
            if owner is self.main_engine:
                break          # MainEngine's own implementation = end of chain
            link = self._next_link(owner, attr)
        return chain

    def _assert_in_chain(self, attr: str, profile: str) -> None:
        chain = self.patch_chain(attr)
        if any(owner is self for owner in chain):
            return
        names = " → ".join(type(owner).__name__ for owner in chain) or "(空)"
        raise RuntimeError(
            f"{profile} 档拒绝启动: main_engine.{attr} 的 patch 链 [{names}] 里没有本 "
            f"RouterEngine —— 该调用绕过了路由(行情不保证走 {self.quote_gateway}、"
            f"订单不保证走 {self.trade_gateway})。成因通常是某个 app 在 Router 之后"
            f"替换了 {attr} 且不转发上一环;若它确实转发,请把捕获到的上一环存成实例"
            f"属性(如 self._{attr}),否则 patch 链无法审计。"
        )

    def verify_patch_chain(self, profile: str) -> None:
        """Called from run_gui AFTER apps are added, BEFORE any gateway
        connects. Fail-closed: raise rather than let the first real order
        discover a broken chain.

        Two things are audited, both by walking the actual chain rather than
        trusting the load order:

        1. LIVE must not have PaperAccountApp loaded — its send_order hijack is
           unconditional and would silently swallow real orders (REQ-010).
        2. The router must still be reachable from the methods it patched.
           Market data is audited in every profile; the order paths only in
           LIVE, because in PAPER the chain legitimately stops at PaperEngine
           (which replaces send_order/send_quote and never forwards — the
           router's forced trade routing is intentionally dead code there).
        """
        live = profile.upper() == "LIVE"
        if live and PAPER_ENGINE_NAME in self.main_engine.engines:
            raise RuntimeError(
                "LIVE 档拒绝启动: PaperAccountApp 已加载 —— 它会无条件劫持 send_order "
                "并吞掉真实订单。LIVE 档禁止加载 PaperAccountApp。"
            )

        for attr in QUOTE_PATH_METHODS:
            self._assert_in_chain(attr, profile)

        paper = self.main_engine.engines.get(PAPER_ENGINE_NAME)
        for attr in TRADE_PATH_METHODS:
            if live:
                self._assert_in_chain(attr, profile)
                continue
            chain = self.patch_chain(attr)
            if any(owner is self for owner in chain):
                continue
            if paper is not None and any(owner is paper for owner in chain):
                continue      # documented PAPER shape: PaperEngine ends the chain
            # Neither the router nor PaperEngine owns the order path: the orders
            # of a session the operator believes is on paper are going somewhere
            # unaudited. Not fatal (PAPER is not the money path) but never silent.
            names = " → ".join(type(owner).__name__ for owner in chain) or "(空)"
            self.main_engine.write_log(
                f"[{profile}] main_engine.{attr} 链 [{names}] 既不经过 Router 也不经过 "
                f"{PAPER_ENGINE_NAME} —— 该路径未被审计,确认它没在下真单",
                APP_NAME,
            )
