"""MarketDataService: owns the market WS, maintains a book per token.

Subscribes to every YES+NO token of the markets we quote and routes each frame
to that token's OrderBook. We do NOT set `custom_feature_enabled` (verified to
broaden the feed beyond our assets); resolution is detected via catalog flags.

On every book mutation it wakes the owning market's quoter via `on_dirty`, and
feeds trade prints to `on_trade` for the flow estimator. Reconnects re-snapshot
automatically because the server sends a fresh `book` on (re)subscribe.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from typing import Any

import websockets

from polymaker.journal import Journal
from polymaker.logging import get_logger
from polymaker.marketdata.orderbook import BookView, OrderBook
from polymaker.marketdata.parse import (
    TradePrint,
    parse_book,
    parse_last_trade,
    parse_price_changes,
    parse_tick_size_change,
)

log = get_logger("marketdata.service")

DirtyCb = Callable[[str, str], None]  # (condition_id, token_id)
TradeCb = Callable[[TradePrint], None]


class MarketDataService:
    def __init__(
        self,
        url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market",
        *,
        on_dirty: DirtyCb | None = None,
        on_trade: TradeCb | None = None,
        journal: Journal | None = None,
        proxy: str | None = None,
    ) -> None:
        self._url = url
        self._on_dirty = on_dirty or (lambda _c, _t: None)
        self._on_trade = on_trade or (lambda _tp: None)
        self._journal = journal
        self._proxy = proxy
        self.books: dict[str, OrderBook] = {}
        self._token_condition: dict[str, str] = {}
        self._subs: list[str] = []
        self._ws: Any = None
        self._stop = asyncio.Event()
        self.connected: bool = False
        # wall-clock when the link last went down (0 until the first run). Used
        # for staleness: a QUIET market with a live link is NOT stale — only a
        # genuinely down connection is. Book-mutation recency can't tell the two
        # apart on a thin market, so we gate on connection liveness instead.
        self.disconnected_since: float = 0.0
        self._needs_snapshot: set[str] = set()
        # redacted health counters (no payloads): distinguish cold empty vs drop
        self.last_message_ts: float = 0.0
        self.last_snapshot_ts: float = 0.0
        self.message_count: int = 0
        self.snapshot_count: int = 0

    # ── subscription management ─────────────────────────────────────────
    def set_markets(self, markets: list[tuple[str, list[str]]]) -> None:
        """markets = [(condition_id, [token_ids...])]. Rebuilds the desired set."""
        subs: list[str] = []
        for cond, tokens in markets:
            for tok in tokens:
                self._token_condition[tok] = cond
                self.books.setdefault(tok, OrderBook())
                self._needs_snapshot.add(tok)
                subs.append(tok)
        self._subs = subs

    def view(self, token_id: str) -> BookView:
        book = self.books.get(token_id)
        return book.view() if book else _empty_view()

    def book(self, token_id: str) -> OrderBook | None:
        return self.books.get(token_id)

    def last_update_ts(self, token_id: str) -> float:
        b = self.books.get(token_id)
        return b.last_update_ts if b else 0.0

    def last_local_ts(self, token_id: str) -> float:
        """Local receive time of the last book mutation (skew-proof staleness)."""
        b = self.books.get(token_id)
        return b.local_ts if b else 0.0

    def is_fresh(self, token_id: str) -> bool:
        """True if this book may be used for quoting.

        A real disconnect clears depth and sets disconnected_since; the book is
        not fresh until a new snapshot arrives. A still-connected quiet book
        that already has depth remains fresh. Unit tests that inject a snapshot
        without starting the WS (disconnected_since == 0) also count as fresh.
        """
        if token_id in self._needs_snapshot:
            return False
        book = self.books.get(token_id)
        has_data = book is not None and (book.last_update_ts > 0.0 or bool(book.bids or book.asks))
        if not has_data:
            return False
        if not self.connected and self.disconnected_since > 0.0:
            return False
        return True

    def is_stale(self, token_id: str) -> bool:
        """True when this book must not be used for quoting."""
        return not self.is_fresh(token_id)

    def needs_snapshot(self, token_id: str) -> bool:
        return token_id in self._needs_snapshot

    def ack_snapshot(self, token_id: str) -> None:
        """Mark that an authoritative book snapshot has been applied."""
        self._needs_snapshot.discard(token_id)

    def health_market_state(self) -> str:
        """Classify market-feed health for local records.

        Cold/empty books on a live connection are ``cold_empty``, never
        ``disconnected``. Snapshot vs message distinguish feed activity.
        """
        if not self.connected:
            return "disconnected"
        if self.last_snapshot_ts > 0.0 and self.last_snapshot_ts + 1e-9 >= self.last_message_ts:
            return "snapshot"
        if self.last_message_ts > 0.0:
            return "message"
        # connected but quiet / no depth yet — not a disconnect
        any_depth = any(bool(b.bids or b.asks) for b in self.books.values())
        if not any_depth:
            return "cold_empty"
        return "connected"

    def clear_books(self) -> None:
        """Drop cached depth and require a fresh snapshot before quoting."""
        self._needs_snapshot.update(self.books.keys())
        for tok in list(self.books.keys()):
            self.books[tok] = OrderBook()
            self._wake(tok)

    def on_disconnect(self) -> None:
        """Handle a real connection drop; do not use this for quiet thin books."""
        self.connected = False
        self.disconnected_since = time.time()
        self.clear_books()

    # ── run loop ────────────────────────────────────────────────────────
    async def run(self) -> None:
        backoff = 1.0
        self.disconnected_since = time.time()  # start the grace clock for first connect
        while not self._stop.is_set():
            try:
                await self._connect_and_listen()
                backoff = 1.0
            except (websockets.ConnectionClosed, OSError) as exc:
                log.warning("market_ws_dropped", err=str(exc), backoff=backoff)
            except Exception as exc:  # noqa: BLE001
                log.error("market_ws_error", err=str(exc))
            if self._stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _connect_and_listen(self) -> None:
        if not self._subs:
            await asyncio.sleep(1.0)
            return
        # ping_timeout matters: with None a half-dead TCP connection hangs
        # forever. The server answers protocol pings (verified live), so a
        # missing pong within 10s means the link is dead -> reconnect.
        kwargs: dict[str, Any] = {"ping_interval": 5, "ping_timeout": 10, "open_timeout": 10}
        if self._proxy:
            kwargs["proxy"] = self._proxy
        async with websockets.connect(self._url, **kwargs) as ws:
            self._ws = ws
            await ws.send(json.dumps({"assets_ids": self._subs, "type": "market"}))
            self.connected = True
            log.info("market_ws_subscribed", n=len(self._subs))
            try:
                async for raw in ws:
                    self._handle(raw)
            finally:
                self.on_disconnect()

    def stop(self) -> None:
        self._stop.set()

    # ── message handling ────────────────────────────────────────────────
    def _handle(self, raw: str | bytes) -> None:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return
        self.last_message_ts = time.time()
        self.message_count += 1
        for msg in data if isinstance(data, list) else [data]:
            if not isinstance(msg, dict):
                continue
            self._dispatch(msg)

    def _dispatch(self, msg: dict[str, Any]) -> None:
        et = msg.get("event_type")
        if et == "book":
            self._on_book(msg)
        elif et == "price_change":
            self._on_price_change(msg)
        elif et == "last_trade_price":
            self._on_last_trade(msg)
        elif et == "tick_size_change":
            self._on_tick_change(msg)

    def _on_book(self, msg: dict[str, Any]) -> None:
        upd = parse_book(msg)
        if upd is None or upd.asset_id not in self.books:
            return
        book = self.books[upd.asset_id]
        if upd.tick_size:
            book.set_tick_size(upd.tick_size)
        book.apply_snapshot(upd.bids, upd.asks, upd.ts, upd.book_hash)
        self.ack_snapshot(upd.asset_id)
        self.last_snapshot_ts = time.time()
        self.snapshot_count += 1
        self._journal_write("book", msg, upd.ts)
        self._wake(upd.asset_id)

    def _on_price_change(self, msg: dict[str, Any]) -> None:
        changes = parse_price_changes(msg)
        touched: set[str] = set()
        for ch in changes:
            book = self.books.get(ch.asset_id)
            if book is None:
                continue
            book.apply_delta(ch.side, ch.price, ch.size, ch.ts)
            touched.add(ch.asset_id)
        if changes:
            self._journal_write("price_change", msg, changes[0].ts)
        for tok in touched:
            self._wake(tok)

    def _on_last_trade(self, msg: dict[str, Any]) -> None:
        tp = parse_last_trade(msg)
        if tp is None or tp.asset_id not in self.books:
            return
        self._journal_write("last_trade_price", msg, tp.ts)
        self._on_trade(tp)

    def _on_tick_change(self, msg: dict[str, Any]) -> None:
        tc = parse_tick_size_change(msg)
        if tc and tc.asset_id in self.books:
            self.books[tc.asset_id].set_tick_size(tc.tick_size)
            log.info("tick_size_change", token=tc.asset_id[:12], tick=tc.tick_size)

    def _wake(self, token_id: str) -> None:
        cond = self._token_condition.get(token_id)
        if cond:
            self._on_dirty(cond, token_id)

    def _journal_write(self, kind: str, payload: dict[str, Any], ts: float) -> None:
        if self._journal is not None:
            self._journal.write(kind, payload, ts)


def _empty_view() -> BookView:
    return BookView(None, 0.0, None, 0.0, None, None, 0.0, 0.0)
