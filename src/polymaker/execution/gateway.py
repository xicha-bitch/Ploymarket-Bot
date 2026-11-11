"""ExecutionGateway: the only component that sends actions to the CLOB.

Wraps the synchronous py-clob-client-v2 (which owns the hard V2 EIP-712 signing,
pUSD balance adjustment, and tick/fee caching) and offloads its blocking network
calls to a thread pool so the asyncio hot path never stalls. Every quote goes out
**post-only** (the maker-only mandate, enforced at the exchange).

Live-only product: place/market-order require LIVE_ENABLE=1 + wallet. Connect and
cancel paths need wallet only so emergency cancel still works when places are gated.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from typing import Any, TypeVar

import httpx

from polymaker.config import Config
from polymaker.domain import MarketMeta, OpenOrder, OrderState, Quote, Side
from polymaker.execution.ratelimit import TokenBucket
from polymaker.journal import Journal
from polymaker.logging import get_logger

log = get_logger("execution.gateway")

_T = TypeVar("_T")

# Data API v2 positions pagination (docs: limit max 1000; cursor is authority).
_POSITIONS_PAGE_LIMIT = 500
_POSITIONS_MAX_PAGES = 50


class PositionsReadError(RuntimeError):
    """Data-API positions read failed — must NOT be treated as an empty book."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after_s = retry_after_s


class OpenOrdersReadError(RuntimeError):
    """CLOB open-orders read failed — must NOT be treated as an empty book."""


class TradesReadError(RuntimeError):
    """CLOB trades read failed — must NOT be treated as an empty fill list."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after_s = retry_after_s


def is_transient_net_error(exc: BaseException) -> bool:
    """SSL / timeout / disconnect style failures that deserve a bounded retry."""
    name = type(exc).__name__
    msg = str(exc).lower()
    needles = (
        "ssl",
        "timeout",
        "timed out",
        "unexpected_eof",
        "connection reset",
        "connection aborted",
        "temporarily unavailable",
        "connect error",
        "request exception",
    )
    if any(n in msg for n in needles):
        return True
    if name in {"ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout"}:
        return True
    # Unwrap httpx / PolyApiException wrappers
    cause = getattr(exc, "__cause__", None)
    if cause is not None and cause is not exc:
        return is_transient_net_error(cause)
    return False


def parse_retry_after_s(raw: str | None) -> float | None:
    """Parse Retry-After as delay-seconds. HTTP-date forms are ignored (None)."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        return None
    if seconds < 0 or seconds != seconds:  # NaN guard
        return None
    return min(seconds, 300.0)


def _tick_str(tick: float) -> str:
    return f"{tick:g}"


def collateral_raw_to_usd(raw: Any) -> float:
    """CLOB collateral `balance` is always micro-units (6 decimals); always /1e6."""
    return float(raw) / 1e6


def parse_v2_positions_page(body: Any) -> tuple[list[Any], str | None]:
    """Parse `{data, pagination}` → (rows, next_cursor_or_None).

    Raises PositionsReadError on unexpected shapes. A successful empty page is
    `data: []` with `next_cursor` null — that is a real zero, not a failure.
    """
    if not isinstance(body, dict) or "data" not in body:
        raise PositionsReadError(
            "unexpected positions response shape (expected v2 {data, pagination})"
        )
    rows = body.get("data")
    if rows is None:
        rows = []
    if not isinstance(rows, list):
        raise PositionsReadError("positions data is not a list")
    pag = body.get("pagination")
    if pag is None:
        pag = {}
    if not isinstance(pag, dict):
        raise PositionsReadError("positions pagination is not an object")
    next_cursor = pag.get("next_cursor")
    has_more = bool(pag.get("has_more"))
    if has_more and not next_cursor:
        raise PositionsReadError("positions page has_more without next_cursor")
    return rows, str(next_cursor) if has_more and next_cursor else None


def map_position_row(row: Any) -> tuple[str, float, float] | None:
    """Map one Data API position row → (token_id, size, avg_price).

    v2 fields: token_id, current_size, avg_price (shares / USDC, not micro-units).
    Legacy v1 names (asset/size/avgPrice) accepted only for test fixtures.
    Dust (size <= 0) is dropped; None means skip, not failure.
    """
    if not isinstance(row, dict):
        return None
    token = row.get("token_id", row.get("asset"))
    if token is None or token == "":
        return None
    try:
        size = float(row.get("current_size", row.get("size", 0)))
        avg = float(row.get("avg_price", row.get("avgPrice", 0)))
    except (TypeError, ValueError):
        return None
    if size <= 0:
        return None
    return str(token), size, avg


class ExecutionGateway:
    def __init__(
        self,
        cfg: Config,
        journal: Journal | None = None,
    ) -> None:
        self._cfg = cfg
        self._journal = journal
        self._client: Any = None  # py_clob_client_v2.ClobClient
        self._creds: Any = None
        self._address: str = ""  # signer EOA
        self._funder: str = ""  # funds/positions live here (proxy/deposit wallet)
        self._data_host = cfg.wallet.data_api_host
        # rate budgets: fraction of documented POST/DELETE ceilings (per second)
        f = cfg.execution.rate_budget_fraction
        self._order_bucket = TokenBucket(rate_per_s=200.0 * f, burst=500.0 * f)
        self._cancel_bucket = TokenBucket(rate_per_s=200.0 * f, burst=500.0 * f)
        self._hb_id: str = ""  # heartbeat chain
        self._hb_failures: int = 0
        self._hb_next_ok_to_try: float = 0.0  # monotonic: bounded backoff gate
        # dedicated, bounded pool for blocking order/HTTP calls so a burst of
        # requotes across many markets can't starve the default executor
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="clob-io")
        self._readonly_attempts: int = 3

    @property
    def order_pressure(self) -> float:
        """0 = plenty of order-post budget, 1 = about to queue (shed load)."""
        return self._order_bucket.pressure

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    async def _io(self, fn: Callable[..., _T], *args: Any) -> _T:
        """Run a blocking client call on the dedicated pool."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, fn, *args)

    async def _io_retry(
        self,
        fn: Callable[..., _T],
        *args: Any,
        attempts: int | None = None,
        label: str = "clob_io",
    ) -> _T:
        """Bounded retry for transient SSL/timeout failures (readonly-safe)."""
        n = self._readonly_attempts if attempts is None else max(1, attempts)
        last: BaseException | None = None
        for i in range(n):
            try:
                return await self._io(fn, *args)
            except Exception as exc:  # noqa: BLE001
                last = exc
                if not is_transient_net_error(exc) or i + 1 >= n:
                    raise
                delay = min(2.0**i, 8.0)
                log.warning(
                    "clob_io_retry",
                    label=label,
                    attempt=i + 1,
                    of=n,
                    sleep_s=delay,
                    err=str(exc)[:160],
                )
                await asyncio.sleep(delay)
        assert last is not None
        raise last

    @property
    def creds(self) -> Any:
        return self._creds

    @property
    def address(self) -> str:
        """The signing EOA address."""
        return self._address

    @property
    def funder(self) -> str:
        """The address holding funds/positions (proxy/deposit wallet, or the EOA)."""
        return self._funder or self._address

    # ── lifecycle ───────────────────────────────────────────────────────
    async def connect(self) -> None:
        """Build the client and derive L2 API creds (network). Requires wallet secrets."""
        sec = self._cfg.secrets
        if not sec.has_wallet:
            raise RuntimeError(
                "no wallet configured (set PK and BROWSER_ADDRESS in local .env)"
            )

        def _build() -> tuple[Any, Any, str]:
            from py_clob_client_v2.client import ClobClient

            client = ClobClient(
                host=self._cfg.wallet.clob_host,
                chain_id=self._cfg.wallet.chain_id,
                key=sec.pk,
                # use_server_time=False: fetching /time before EVERY signed order
                # adds a full round-trip per op (latency killer through a proxy).
                # We check clock drift once below and rely on the local (NTP) clock.
                signature_type=self._cfg.wallet.signature_type,
                funder=sec.browser_address,
            )
            creds = client.create_or_derive_api_key()
            client.set_api_creds(creds)
            return client, creds, client.get_address()

        self._client, self._creds, self._address = await self._io(_build)
        await self._check_clock_drift()
        # funds/positions live on the funder (proxy/deposit wallet); fall back to EOA
        self._funder = sec.browser_address or self._address
        log.info("gateway_connected", signer=self._address[:10], funder=self._funder[:10])

    async def _check_clock_drift(self) -> None:
        """Warn once if the local clock is skewed vs the exchange (affects L2 auth)."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                r = await c.get(f"{self._cfg.wallet.clob_host}/time")
                server = float(r.text.strip().strip('"'))
            drift = abs(time.time() - server)
            if drift > 5.0:
                log.warning("clock_drift", drift_s=round(drift, 1),
                            note="sync system clock (NTP) — large skew can fail order auth")
            else:
                log.info("clock_ok", drift_s=round(drift, 1))
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("clock_check_failed", err=str(exc))

    def _assert_place_gate(self) -> None:
        """Fail closed: CLOB place/market-order unreachable without LIVE_ENABLE=1."""
        from polymaker.live_gate import assert_live_place_allowed

        assert_live_place_allowed(self._cfg)

    # ── placement ───────────────────────────────────────────────────────
    async def place(self, quotes: list[Quote], meta: MarketMeta) -> list[OpenOrder]:
        if not quotes:
            return []
        self._assert_place_gate()
        if self._client is None:
            log.error("place_failed", err="not connected", n=len(quotes))
            return []
        await self._order_bucket.acquire(len(quotes))
        ts = time.time()
        self._journal_write("orders_out", [asdict(q) for q in quotes], ts)

        def _place() -> list[OpenOrder]:
            from py_clob_client_v2.clob_types import (
                OrderArgsV2,
                OrderType,
                PartialCreateOrderOptions,
                PostOrdersV2Args,
            )

            opts = PartialCreateOrderOptions(tick_size=_tick_str(meta.tick_size), neg_risk=meta.neg_risk)
            args = []
            for q in quotes:
                signed = self._client.create_order(
                    OrderArgsV2(token_id=q.token_id, price=q.price, size=q.size, side=q.side.value),
                    options=opts,
                )
                args.append(PostOrdersV2Args(order=signed, orderType=OrderType.GTC))
            resp = self._client.post_orders(args, post_only=self._cfg.execution.post_only)
            return self._parse_place_response(resp, quotes)

        try:
            return await self._io(_place)
        except Exception as exc:  # noqa: BLE001 - surface + continue; engine handles error rate
            log.error("place_failed", err=str(exc), n=len(quotes))
            return []

    def _parse_place_response(self, resp: Any, quotes: list[Quote]) -> list[OpenOrder]:
        """Map a batch post response to OpenOrders. Tolerant of shape variants;
        the user-WS order events + REST snapshot reconcile anything we miss."""
        items = resp if isinstance(resp, list) else resp.get("orders", resp.get("data", []))
        out: list[OpenOrder] = []
        for q, item in zip(quotes, items if isinstance(items, list) else [], strict=False):
            oid = _first(item, "orderID", "orderId", "order_id", "id", "hash")
            if not oid:
                log.warning("place_response_missing_id", item=str(item)[:120])
                continue
            out.append(OpenOrder(str(oid), q.token_id, q.side, q.price, q.size, OrderState.LIVE))
        return out

    # ── cancellation ────────────────────────────────────────────────────
    async def cancel(self, order_ids: list[str]) -> bool:
        """Cancel by id. Returns True on success — callers must NOT drop the
        orders from local state on failure (they may still be live)."""
        if not order_ids:
            return True
        if self._client is None:
            return False
        await self._cancel_bucket.acquire(1)

        def _cancel() -> None:
            self._client.cancel_orders(order_ids)

        try:
            await self._io(_cancel)
            return True
        except Exception as exc:  # noqa: BLE001
            log.error("cancel_failed", err=str(exc), n=len(order_ids))
            return False

    async def cancel_asset(self, asset_id: str) -> bool:
        """Cancel every order on one token (idempotent quarantine primitive)."""
        if self._client is None:
            return False

        def _cancel() -> None:
            from py_clob_client_v2.clob_types import OrderMarketCancelParams

            self._client.cancel_market_orders(OrderMarketCancelParams(asset_id=asset_id))

        try:
            await self._io(_cancel)
            return True
        except Exception as exc:  # noqa: BLE001
            log.error("cancel_asset_failed", err=str(exc), token=asset_id[:12])
            return False

    async def cancel_all(self) -> None:
        if self._client is None:
            return
        await self._io(self._client.cancel_all)
        log.info("cancel_all_sent")

    # ── market (taker) orders — used by moneydoctor, NOT the maker strategy ──
    async def market_order(
        self, token_id: str, side: Side, amount: float, meta: MarketMeta,
        *, fak: bool = True,
    ) -> dict[str, Any]:
        """Place a marketable order. amount = USD for BUY, shares for SELL.

        This is a TAKER order (crosses the spread) — only the moneydoctor live
        self-test uses it; the maker strategy never does.
        """
        from polymaker.live_gate import LiveGateError

        try:
            self._assert_place_gate()
        except LiveGateError as exc:
            return {"status": "failed", "error": str(exc)}
        if self._client is None:
            return {"status": "failed", "error": "not connected"}

        def _do() -> dict[str, Any]:
            from py_clob_client_v2.clob_types import (
                MarketOrderArgsV2,
                OrderType,
                PartialCreateOrderOptions,
            )

            ot = OrderType.FAK if fak else OrderType.FOK
            args = MarketOrderArgsV2(token_id=token_id, amount=amount,
                                     side=side.value, order_type=ot)
            opts = PartialCreateOrderOptions(tick_size=_tick_str(meta.tick_size),
                                             neg_risk=meta.neg_risk)
            try:
                resp = self._client.create_and_post_market_order(args, opts, order_type=ot)
                return resp if isinstance(resp, dict) else {"resp": resp}
            except Exception as exc:  # noqa: BLE001 - surface as data, never crash the caller
                return {"status": "failed", "error": str(exc)}

        return await self._io(_do)

    async def get_book(self, token_id: str) -> dict[str, float]:
        """Live best bid/ask + touch depth for one token (public REST)."""
        try:
            async with httpx.AsyncClient(timeout=15.0) as c:
                r = await c.get(f"{self._cfg.wallet.clob_host}/book",
                                params={"token_id": token_id})
                r.raise_for_status()
                b = r.json()
                bids = [(float(x["price"]), float(x["size"])) for x in b.get("bids", [])]
                asks = [(float(x["price"]), float(x["size"])) for x in b.get("asks", [])]
                best_bid = max(bids)[0] if bids else 0.0
                best_ask = min(asks)[0] if asks else 1.0
                ask_depth = sum(s for p, s in asks if p <= best_ask + 1e-9)
                bid_depth = sum(s for p, s in bids if p >= best_bid - 1e-9)
                return {"best_bid": best_bid, "best_ask": best_ask,
                        "ask_depth": ask_depth, "bid_depth": bid_depth}
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            log.warning("get_book_failed", err=str(exc))
            return {}

    async def get_full_book(
        self, token_id: str
    ) -> tuple[list[tuple[float, float]], list[tuple[float, float]], str | None] | None:
        """Full L2 book (bids, asks, hash) via public REST — for periodic
        integrity refresh against the WS book."""
        try:
            async with httpx.AsyncClient(timeout=15.0) as c:
                r = await c.get(f"{self._cfg.wallet.clob_host}/book",
                                params={"token_id": token_id})
                r.raise_for_status()
                b = r.json()
                bids = [(float(x["price"]), float(x["size"])) for x in b.get("bids", [])]
                asks = [(float(x["price"]), float(x["size"])) for x in b.get("asks", [])]
                return bids, asks, b.get("hash")
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            log.warning("get_full_book_failed", err=str(exc))
            return None

    async def token_balance(self, token_id: str) -> float:
        """Exact on-chain conditional-token balance (shares) held by the funder.

        Returns None on total RPC failure so callers can distinguish "0 shares"
        from "couldn't read".
        """
        bal = await self._token_balance_opt(token_id)
        return bal if bal is not None else 0.0

    async def _token_balance_opt(self, token_id: str) -> float | None:
        def _read() -> float | None:
            from web3 import Web3
            from web3.middleware import ExtraDataToPOAMiddleware

            configured = self._cfg.secrets.polygon_rpc or self._cfg.wallet.polygon_rpc
            rpcs = [configured, "https://polygon-bor-rpc.publicnode.com",
                    "https://polygon.llamarpc.com", "https://rpc.ankr.com/polygon"]
            abi = [{"name": "balanceOf", "type": "function", "stateMutability": "view",
                    "inputs": [{"name": "a", "type": "address"}, {"name": "id", "type": "uint256"}],
                    "outputs": [{"name": "", "type": "uint256"}]}]
            for rpc in dict.fromkeys(rpcs):  # dedupe, keep order
                try:
                    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 15}))
                    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
                    ctf = w3.eth.contract(
                        address=Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"),
                        abi=abi,
                    )
                    raw = ctf.functions.balanceOf(
                        Web3.to_checksum_address(self.funder), int(token_id)
                    ).call()
                    return float(raw) / 1e6
                except Exception:  # noqa: BLE001, PERF203 - try next RPC
                    continue
            return None

        try:
            return await self._io(_read)
        except Exception as exc:  # noqa: BLE001
            log.warning("token_balance_failed", err=str(exc))
            return None

    async def token_balances(self, token_ids: list[str]) -> dict[str, float] | None:
        """Batch on-chain balances for several tokens in one RPC session.

        Used by the position-divergence monitor. Returns None on RPC failure.
        """
        if not token_ids:
            return {}

        def _read() -> dict[str, float] | None:
            from web3 import Web3
            from web3.middleware import ExtraDataToPOAMiddleware

            configured = self._cfg.secrets.polygon_rpc or self._cfg.wallet.polygon_rpc
            rpcs = [configured, "https://polygon-bor-rpc.publicnode.com",
                    "https://polygon.llamarpc.com", "https://rpc.ankr.com/polygon"]
            abi = [{"name": "balanceOf", "type": "function", "stateMutability": "view",
                    "inputs": [{"name": "a", "type": "address"}, {"name": "id", "type": "uint256"}],
                    "outputs": [{"name": "", "type": "uint256"}]}]
            funder = None
            for rpc in dict.fromkeys(rpcs):
                try:
                    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 20}))
                    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
                    ctf = w3.eth.contract(
                        address=Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"),
                        abi=abi,
                    )
                    funder = Web3.to_checksum_address(self.funder)
                    out: dict[str, float] = {}
                    for tid in token_ids:
                        raw = ctf.functions.balanceOf(funder, int(tid)).call()
                        out[tid] = float(raw) / 1e6
                    return out
                except Exception:  # noqa: BLE001, PERF203
                    continue
            return None

        try:
            return await self._io(_read)
        except Exception as exc:  # noqa: BLE001
            log.warning("token_balances_failed", err=str(exc))
            return None

    async def collateral_balance(self) -> float:
        """pUSD balance (float) on the funder. CLOB raw is always /1e6 micro-units."""
        ba = await self.balance_allowance()
        for k in ("balance", "collateral", "amount"):
            if isinstance(ba, dict) and k in ba:
                try:
                    return collateral_raw_to_usd(ba[k])
                except (ValueError, TypeError):
                    return 0.0
        return 0.0

    # ── heartbeat (dead-man switch) ─────────────────────────────────────
    async def heartbeat(self) -> bool:
        """Send one chained heartbeat. Returns True on success.

        The exchange expects each heartbeat to carry the previous heartbeat_id.
        Consecutive failures are tracked in `heartbeat_failures`: after enough
        misses the exchange auto-cancels ALL our orders, so the engine must
        stop quoting and resync once the heartbeat recovers.

        Failures use bounded exponential backoff (``heartbeat_backoff_s``) so
        SSL/timeout storms do not hammer the endpoint every interval tick.
        """
        if self._client is None:
            self._hb_failures += 1
            return False

        now = time.monotonic()
        if now < self._hb_next_ok_to_try:
            # Still in backoff window from a prior failure — count as soft miss
            # without another network call.
            self._hb_failures += 1
            return False

        def _beat() -> Any:
            return self._client.post_heartbeat(self._hb_id)

        try:
            # One bounded retry on transient SSL/timeout for the beat itself.
            resp = await self._io_retry(_beat, attempts=2, label="heartbeat")
            new_id = _first(resp, "heartbeat_id", "heartbeatId", "id")
            self._hb_id = str(new_id) if new_id else ""
            if self._hb_failures:
                log.info("heartbeat_recovered", after_failures=self._hb_failures)
            self._hb_failures = 0
            self._hb_next_ok_to_try = 0.0
            return True
        except Exception as exc:  # noqa: BLE001
            self._hb_failures += 1
            self._hb_id = ""  # broken chain — restart it
            backoff = min(2.0 ** min(self._hb_failures, 6), 60.0)
            self._hb_next_ok_to_try = time.monotonic() + backoff
            log.warning(
                "heartbeat_failed",
                err=str(exc)[:160],
                consecutive=self._hb_failures,
                backoff_s=round(backoff, 2),
            )
            return False

    @property
    def heartbeat_failures(self) -> int:
        return self._hb_failures

    @property
    def heartbeat_backoff_s(self) -> float:
        """Seconds remaining before the next heartbeat network attempt."""
        rem = self._hb_next_ok_to_try - time.monotonic()
        return max(0.0, rem)

    # ── reads ───────────────────────────────────────────────────────────
    async def open_orders(self) -> list[OpenOrder]:
        """Return live open orders. Empty list means confirmed none.

        Transport / client failures raise OpenOrdersReadError — callers must
        not treat that as an empty book (exit-path / reconcile correctness).
        """
        if self._client is None:
            raise OpenOrdersReadError("open_orders unavailable: client not connected")

        def _get() -> list[OpenOrder]:
            raw = self._client.get_open_orders()
            rows = raw if isinstance(raw, list) else raw.get("data", raw.get("orders", []))
            out = []
            for r in rows:
                try:
                    side = Side(str(r["side"]).upper())
                    remaining = float(r.get("original_size", r.get("size", 0))) - float(
                        r.get("size_matched", 0)
                    )
                    out.append(
                        OpenOrder(
                            str(_first(r, "id", "orderID", "order_id")),
                            str(r["asset_id"]),
                            side,
                            float(r["price"]),
                            remaining,
                            OrderState.LIVE,
                        )
                    )
                except (KeyError, ValueError, TypeError):
                    continue
            return out

        try:
            return await self._io_retry(_get, label="open_orders")
        except OpenOrdersReadError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("open_orders_failed", err=str(exc))
            raise OpenOrdersReadError(f"open_orders read failed: {exc}") from exc

    async def get_trades(
        self,
        *,
        after_ts: int | None = None,
        before_ts: int | None = None,
        asset_id: str | None = None,
        market: str | None = None,
        only_first_page: bool = False,
    ) -> list[dict[str, Any]]:
        """Authenticated trade history. Empty list means confirmed none.

        Transport / TLS / HTTP failures raise TradesReadError — callers must
        NOT treat that as zero fills (unknown ≠ empty).
        """
        if self._client is None:
            raise TradesReadError("trades unavailable: client not connected")

        def _get() -> list[dict[str, Any]]:
            from py_clob_client_v2.clob_types import TradeParams

            params = TradeParams(
                maker_address=self.address or None,
                after=after_ts,
                before=before_ts,
                asset_id=asset_id,
                market=market,
            )
            raw = self._client.get_trades(params, only_first_page=only_first_page)
            if raw is None:
                return []
            if not isinstance(raw, list):
                raise TradesReadError("unexpected get_trades response shape")
            out: list[dict[str, Any]] = []
            for row in raw:
                if isinstance(row, dict):
                    out.append(row)
            return out

        try:
            return await self._io_retry(_get, label="get_trades")
        except TradesReadError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("get_trades_failed", err=str(exc)[:160])
            raise TradesReadError(f"trades read failed: {exc}") from exc

    async def positions(self) -> dict[str, tuple[float, float]]:
        """{token_id: (size, avg_price)} from Data API v2 (reconcile use).

        Queries the FUNDER / proxy wallet (where positions live), not the signer
        EOA. Uses `GET {data_api_host}/v2/positions` and follows cursor pages.

        A successful empty book returns `{}`. HTTP 410/non-200, bad envelopes,
        or transport errors raise PositionsReadError — callers must NOT treat
        that as zero inventory.
        """
        user = self.funder
        if not user or not str(user).startswith("0x") or user == "0xPAPER":
            raise PositionsReadError("no funder address for positions read")

        out: dict[str, tuple[float, float]] = {}
        cursor: str | None = None
        last_transient: Exception | None = None
        for attempt in range(self._readonly_attempts):
            out = {}
            cursor = None
            try:
                async with httpx.AsyncClient(timeout=15.0) as c:
                    for _page in range(_POSITIONS_MAX_PAGES):
                        params: dict[str, Any] = {"user": user}
                        if cursor:
                            params["cursor"] = cursor
                        else:
                            params["limit"] = _POSITIONS_PAGE_LIMIT
                        url = f"{self._data_host}/v2/positions"
                        r = await c.get(url, params=params)
                        if r.status_code != 200:
                            body = ""
                            with contextlib.suppress(Exception):
                                body = (r.text or "")[:180]
                            retry_after_s = parse_retry_after_s(r.headers.get("Retry-After"))
                            log.warning(
                                "positions_failed",
                                status=r.status_code,
                                host=self._data_host,
                                path="/v2/positions",
                                # funder prefix only — never full keys/secrets
                                funder=user[:10],
                                retry_after_s=retry_after_s,
                                detail=body,
                            )
                            raise PositionsReadError(
                                f"data-api GET /v2/positions HTTP {r.status_code}: {body}",
                                status_code=r.status_code,
                                retry_after_s=retry_after_s,
                            )
                        rows, cursor = parse_v2_positions_page(r.json())
                        for row in rows:
                            mapped = map_position_row(row)
                            if mapped is None:
                                continue
                            token_id, size, avg = mapped
                            out[token_id] = (size, avg)
                        if cursor is None:
                            return out
                    raise PositionsReadError(
                        f"positions pagination exceeded {_POSITIONS_MAX_PAGES} pages"
                    )
            except PositionsReadError:
                raise
            except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
                last_transient = exc
                if not is_transient_net_error(exc) or attempt + 1 >= self._readonly_attempts:
                    log.warning("positions_failed", err=str(exc), funder=user[:10])
                    raise PositionsReadError(f"positions read failed: {exc}") from exc
                delay = min(2.0**attempt, 8.0)
                log.warning(
                    "positions_retry",
                    attempt=attempt + 1,
                    sleep_s=delay,
                    err=str(exc)[:160],
                    funder=user[:10],
                )
                await asyncio.sleep(delay)
        assert last_transient is not None
        raise PositionsReadError(
            f"positions read failed: {last_transient}"
        ) from last_transient

    async def portfolio_value(self) -> float | None:
        """Mark-to-market position value (USDC) for the funder via Data API v2.

        `GET /v2/value?user=<funder>`. Returns None on failure (not zero).
        """
        user = self.funder
        if not user or not str(user).startswith("0x") or user == "0xPAPER":
            return None
        try:
            async with httpx.AsyncClient(timeout=15.0) as c:
                r = await c.get(f"{self._data_host}/v2/value", params={"user": user})
                if r.status_code != 200:
                    log.warning(
                        "portfolio_value_failed",
                        status=r.status_code,
                        funder=user[:10],
                    )
                    return None
                body = r.json()
                data = body.get("data") if isinstance(body, dict) else None
                if not isinstance(data, dict) or "value" not in data:
                    return None
                return float(data["value"])
        except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
            log.warning("portfolio_value_failed", err=str(exc), funder=user[:10])
            return None

    async def balance_allowance(self) -> dict[str, Any]:
        """Collateral balance/allowance snapshot (for `doctor`)."""
        if self._client is None:
            return {}

        def _get() -> dict[str, Any]:
            from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

            result: dict[str, Any] = self._client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            return result

        try:
            return await self._io(_get)
        except Exception as exc:  # noqa: BLE001
            log.warning("balance_allowance_failed", err=str(exc))
            return {}

    def _journal_write(self, kind: str, payload: Any, ts: float) -> None:
        if self._journal is not None:
            self._journal.write(kind, payload, ts)


def _first(d: Any, *keys: str) -> Any:
    if not isinstance(d, dict):
        return None
    for k in keys:
        if k in d and d[k]:
            return d[k]
    return None
