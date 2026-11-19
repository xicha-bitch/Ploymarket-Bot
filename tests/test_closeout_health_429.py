"""Closeout: exit-path status, positions 429 backoff recovery, health records."""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest
import respx

from polymaker.domain import OpenOrder, OrderState, Side
from polymaker.execution.gateway import (
    OpenOrdersReadError,
    PositionsReadError,
    parse_retry_after_s,
)
from polymaker.marketdata.service import MarketDataService
from polymaker.state.store import StateStore
from tests.stub_gateway import StubGateway
from tests.test_engine import _engine_with_market
from tests.test_positions_readout import DATA_HOST, _gw


def test_parse_retry_after_seconds_and_rejects_junk() -> None:
    assert parse_retry_after_s("7") == 7.0
    assert parse_retry_after_s("1.5") == 1.5
    assert parse_retry_after_s(None) is None
    assert parse_retry_after_s("") is None
    assert parse_retry_after_s("Fri, 31 Dec 1999 23:59:59 GMT") is None
    assert parse_retry_after_s("-1") is None


@respx.mock
def test_positions_429_honors_retry_after_header() -> None:
    respx.get(f"{DATA_HOST}/v2/positions").mock(
        return_value=httpx.Response(
            429,
            headers={"Retry-After": "12"},
            json={"error": "rate limited"},
        )
    )
    with pytest.raises(PositionsReadError) as ei:
        asyncio.run(_gw().positions())
    assert ei.value.status_code == 429
    assert ei.value.retry_after_s == 12.0


@respx.mock
def test_positions_429_without_retry_after_still_raises() -> None:
    respx.get(f"{DATA_HOST}/v2/positions").mock(
        return_value=httpx.Response(429, json={"error": "slow down"})
    )
    with pytest.raises(PositionsReadError) as ei:
        asyncio.run(_gw().positions())
    assert ei.value.status_code == 429
    assert ei.value.retry_after_s is None


async def test_reconcile_positions_429_backoff_then_recover(tmp_path, meta) -> None:
    eng = _engine_with_market(tmp_path, meta)
    eng.cfg.engine.reconcile_interval_s = 0.05
    eng.state.set_position(meta.yes.token_id, 9.99, 0.0475)

    class Flaky(StubGateway):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def positions(self):
            self.calls += 1
            if self.calls == 1:
                raise PositionsReadError(
                    "HTTP 429", status_code=429, retry_after_s=0.15
                )
            return {meta.yes.token_id: (9.99, 0.0475)}

        async def open_orders(self):
            return []

    gw = Flaky()
    eng.gateway = gw  # type: ignore[assignment]
    eng._running = True
    task = asyncio.create_task(eng._reconcile_loop())
    try:
        await asyncio.sleep(0.08)
        assert eng._positions_healthy is False
        assert eng._positions_fail_streak >= 1
        assert eng.state.position(meta.yes.token_id).size == pytest.approx(9.99)
        # still inside Retry-After window — no storm
        calls_during_backoff = gw.calls
        await asyncio.sleep(0.08)
        assert gw.calls == calls_during_backoff
        # after backoff, next cadence recovers
        await asyncio.sleep(0.25)
        assert eng._positions_healthy is True
        assert eng._positions_fail_streak == 0
        assert gw.calls >= 2
        rows = eng.state.recent_health(limit=5)
        assert len(rows) >= 2
        assert rows[0]["seq"] > rows[1]["seq"]
        assert rows[0]["ts"] >= rows[1]["ts"]
    finally:
        eng._running = False
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        eng.state.close()
        eng.catalog.close()


async def test_telegram_status_exit_order_resting_vs_unknown(tmp_path, meta) -> None:
    eng = _engine_with_market(tmp_path, meta)
    eng.state.set_position(meta.yes.token_id, 9.99, 0.05)

    class WithSell(StubGateway):
        async def open_orders(self):
            return [
                OpenOrder(
                    "ord-sell-1",
                    meta.yes.token_id,
                    Side.SELL,
                    0.051,
                    9.99,
                    OrderState.LIVE,
                )
            ]

        async def portfolio_value(self):
            return 0.5

    eng.gateway = WithSell()  # type: ignore[assignment]
    text = await eng._telegram_status_text()
    assert "退出报价：是" in text
    assert "有(SELL=1)" in text
    assert "日亏锁：" in text
    assert "恢复阻断：" in text

    class BoomOrders(StubGateway):
        async def open_orders(self):
            raise OpenOrdersReadError("net down")

        async def portfolio_value(self):
            return 0.5

    eng.gateway = BoomOrders()  # type: ignore[assignment]
    text2 = await eng._telegram_status_text()
    assert "退出报价：是" in text2
    assert "未知（不视为仍保留）" in text2
    assert "有(SELL=" not in text2
    eng.state.close()
    eng.catalog.close()


def test_health_records_advance_and_redact(tmp_path) -> None:
    s = StateStore(tmp_path / "h.db")
    a = s.record_health(
        heartbeat="ok",
        market="cold_empty",
        user_stream="connected",
        telegram_poll="ok",
        reconcile="ok",
        detail={"orders_n": 1, "token_prefix": "1454697840"},
        ts=1000.0,
    )
    b = s.record_health(
        heartbeat="ok",
        market="snapshot",
        user_stream="connected",
        telegram_poll="ok",
        reconcile="ok",
        detail={"orders_n": 1, "pos_n": 1},
        ts=1001.0,
    )
    assert b > a > 0
    rows = s.recent_health(limit=2)
    assert len(rows) == 2
    assert rows[0]["seq"] == b
    assert rows[1]["seq"] == a
    assert rows[0]["ts"] > rows[1]["ts"]
    blob = str(rows)
    assert "api_key" not in blob.lower()
    assert "secret" not in blob.lower()
    assert "token=" not in blob.lower()
    s.close()


async def test_positions_429_blind_pulls_then_recovery_requotes(tmp_path, meta) -> None:
    from tests.test_engine import _feed_book

    eng = _engine_with_market(tmp_path, meta)
    eng._trades_backfill_ok = True
    eng._user_started = False
    _feed_book(eng, meta)
    await eng._recompute(meta.condition_id)
    assert len(eng.state.orders) > 0
    placed_before = len(eng.gateway.placed)  # type: ignore[attr-defined]

    eng._positions_healthy = False
    await eng._recompute(meta.condition_id)
    assert len(eng.state.orders) == 0

    eng._positions_healthy = True
    eng._maybe_requote_after_recovery_gate(reason="positions_reconcile_recovered")
    await eng._recompute(meta.condition_id)
    assert len(eng.state.orders) > 0
    assert len(eng.gateway.placed) > placed_before  # type: ignore[attr-defined]
    eng.state.close()
    eng.catalog.close()


async def test_positions_still_429_does_not_requote(tmp_path, meta) -> None:
    from tests.test_engine import _feed_book

    eng = _engine_with_market(tmp_path, meta)
    eng._trades_backfill_ok = True
    eng._user_started = False
    _feed_book(eng, meta)
    await eng._recompute(meta.condition_id)
    placed_before = len(eng.gateway.placed)  # type: ignore[attr-defined]

    eng._positions_healthy = False
    await eng._recompute(meta.condition_id)
    assert len(eng.state.orders) == 0

    # Gate still blind — must not repost even if quoter wakes.
    eng._maybe_requote_after_recovery_gate(reason="positions_reconcile_recovered")
    await eng._recompute(meta.condition_id)
    assert len(eng.state.orders) == 0
    assert len(eng.gateway.placed) == placed_before  # type: ignore[attr-defined]
    eng.state.close()
    eng.catalog.close()


async def test_reconcile_positions_recovered_requotes_via_loop(tmp_path, meta) -> None:
    from tests.test_engine import _feed_book

    eng = _engine_with_market(tmp_path, meta)
    eng.cfg.engine.reconcile_interval_s = 0.05
    eng._trades_backfill_ok = True
    eng._user_started = False
    _feed_book(eng, meta)
    await eng._recompute(meta.condition_id)
    assert len(eng.state.orders) > 0

    class Flaky(StubGateway):
        def __init__(self) -> None:
            super().__init__()
            self.fail = True

        async def positions(self):
            if self.fail:
                raise PositionsReadError(
                    "HTTP 429", status_code=429, retry_after_s=0.08
                )
            return {}

    flaky = Flaky()
    eng.gateway = flaky  # type: ignore[assignment]
    eng._running = True
    task = asyncio.create_task(eng._reconcile_loop())
    try:
        await asyncio.sleep(0.12)
        assert eng._positions_healthy is False
        await eng._recompute(meta.condition_id)
        assert len(eng.state.orders) == 0
        flaky.fail = False
        await asyncio.sleep(0.25)
        assert eng._positions_healthy is True
        await eng._recompute(meta.condition_id)
        assert len(eng.state.orders) > 0
    finally:
        eng._running = False
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        eng.state.close()
        eng.catalog.close()


def test_market_cold_empty_not_disconnected() -> None:
    md = MarketDataService()
    md.connected = True
    md.disconnected_since = 0.0
    assert md.health_market_state() == "cold_empty"
    md.connected = False
    md.disconnected_since = time.time()
    assert md.health_market_state() == "disconnected"
    md.connected = True
    md.last_message_ts = time.time()
    md.last_snapshot_ts = md.last_message_ts + 0.001
    assert md.health_market_state() == "snapshot"
    md.last_message_ts = time.time() + 1
    assert md.health_market_state() == "message"
