"""Regression: fill/cash ledger + daily_loss false-positive + recovery gates.

Covers the 2026-11-10 inventory-zero → invented daily_loss pattern without
inventing fills or auto-clearing on transport failure.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from polymaker.config import RiskConfig
from polymaker.domain import Fill, Side
from polymaker.execution.gateway import (
    OpenOrdersReadError,
    PositionsReadError,
    TradesReadError,
    is_transient_net_error,
)
from polymaker.execution.trades import fills_from_rest_trade
from polymaker.risk.manager import RiskManager, shanghai_day_key
from polymaker.state.store import StateStore
from tests.stub_gateway import StubGateway
from tests.test_engine import _engine_with_market


def _rm(tmp_path, **over):
    cfg = RiskConfig(
        **{
            "max_total_exposure_usdc": 5000,
            "max_market_notional_usdc": 800,
            "max_event_group_loss_usdc": 1000,
            "daily_loss_kill_usdc": 0.2,
            **over,
        }
    )
    store = StateStore(tmp_path / "s.db")
    return RiskManager(cfg, store), store


def test_asia_shanghai_zoneinfo_requires_tzdata() -> None:
    """Windows ships no system zoneinfo; tzdata must provide Asia/Shanghai.

    Missing tzdata raises ZoneInfoNotFoundError at import/startup (b0dea54 crash).
    """
    import tzdata  # noqa: F401 — runtime dep declared in pyproject

    tz = ZoneInfo("Asia/Shanghai")
    assert str(tz) == "Asia/Shanghai"
    key = shanghai_day_key(1_700_000_000.0)  # uses Asia/Shanghai
    assert key == "2023-11-15"
    # day-boundary helper must construct the same zone without OS tz files
    assert datetime.now(tz=tz).tzinfo is not None


def test_rest_flatten_without_fill_does_not_invent_daily_loss(tmp_path, meta):
    """Incident shape: inventory MTM vanishes, net_cash stays 0, fills empty."""
    rm, store = _rm(tmp_path)
    tok = meta.yes.token_id
    store.set_position(tok, 9.85, 0.05)
    rm.update_mark(tok, 0.05)
    rm.reset_day()
    # Small positive daily from mark noise
    rm.update_mark(tok, 0.05)
    assert rm.daily_pnl == pytest.approx(0.0, abs=1e-9)
    assert rm.global_halt()[0] is False

    # REST snapshot zeros inventory with no fill / no cash credit
    deltas = store.reconcile_positions({}, is_snapshot=True)
    assert deltas and deltas[0][2] == 0.0
    for token_id, old_s, new_s, unit in deltas:
        rm.note_unexplained_inventory_delta((new_s - old_s) * rm.unit_value(token_id, unit))

    assert store.fill_count() == 0
    assert rm.net_cash == 0.0
    assert rm.conserved_usdc == pytest.approx(9.85 * 0.05)
    assert rm.equity == pytest.approx(rm.day_start_equity)
    assert rm.daily_pnl == pytest.approx(0.0, abs=1e-9)
    assert rm.global_halt()[0] is False
    store.close()


def test_user_stream_miss_then_rest_fill_dedupe(tmp_path, meta):
    """WS miss + REST backfill applies once; replay is ignored."""
    rm, store = _rm(tmp_path)
    tok = meta.yes.token_id
    fill = Fill(tok, Side.SELL, 0.049, 9.84, "trade-abc", ts=1.0)
    assert store.apply_fill(fill) is True
    rm.note_fill(fill)
    cash_after_first = rm.net_cash
    assert cash_after_first == pytest.approx(0.049 * 9.84)
    # Engine skips note_fill when apply_fill returns False (dedupe gate).
    assert store.apply_fill(fill) is False
    assert rm.net_cash == cash_after_first
    store.close()


def test_partial_fill_dedupe_ids(tmp_path):
    s = StateStore(tmp_path / "p.db")
    f1 = Fill("tok", Side.SELL, 0.049, 9.98, "ord-partial:1")
    f2 = Fill("tok", Side.SELL, 0.049, 0.01, "ord-partial:2")
    assert s.apply_fill(f1) and s.apply_fill(f2)
    assert s.apply_fill(f1) is False
    assert s.position("tok").size == 0.0
    assert s.fill_count() == 2
    s.close()


def test_cross_day_baseline_rolls_and_clears_latch(tmp_path, meta):
    rm, store = _rm(tmp_path)
    rm.reset_day()
    # Force a latched loss via mark crash with filled inventory
    store.apply_fill(Fill(meta.yes.token_id, Side.BUY, 0.50, 2.0, "t1"))
    rm.note_fill(Fill(meta.yes.token_id, Side.BUY, 0.50, 2.0, "t1"))
    rm.update_mark(meta.yes.token_id, 0.50)
    rm.reset_day()
    rm.update_mark(meta.yes.token_id, 0.01)  # unrealized −0.98
    halted, why = rm.global_halt()
    assert halted and "daily_loss" in why
    assert rm.daily_loss_latched is True

    # Same day: roll is no-op, latch remains
    assert rm.roll_day_if_needed() is False
    assert rm.daily_loss_latched is True

    # Simulate next Shanghai day by rewriting day_key then rolling
    yesterday = (
        datetime.now(tz=ZoneInfo("Asia/Shanghai")) - timedelta(days=1)
    ).strftime("%Y-%m-%d")
    rm._day_key = yesterday
    assert rm.roll_day_if_needed() is True
    assert rm.daily_loss_latched is False
    assert rm.day_key == shanghai_day_key()
    store.close()


def test_restart_same_day_preserves_latch_from_snapshot(tmp_path, meta):
    """Restart must not wipe an in-day daily_loss via reset_day."""
    db = tmp_path / "r.db"
    store = StateStore(db)
    # Pre-persistence incident shape: pnl snapshot only (no risk_day row yet).
    store.record_pnl(0.0, 0.0, 0.0, -0.474525)
    store.close()

    store2 = StateStore(db)
    rm2 = RiskManager(RiskConfig(daily_loss_kill_usdc=0.2), store2)
    rm2.bootstrap_session()
    assert rm2.daily_loss_latched is True
    assert rm2.global_halt()[0] is True
    # Must NOT invent cash from the snapshot daily_pnl figure
    assert rm2.net_cash == 0.0
    assert store2.fills_count() == 0
    store2.close()

    # Same-day restart with persisted risk_day must keep the latch.
    store3 = StateStore(db)
    rm3 = RiskManager(RiskConfig(daily_loss_kill_usdc=0.2), store3)
    rm3.bootstrap_session()
    assert rm3.daily_loss_latched is True
    assert rm3.global_halt()[0] is True
    store3.close()


def test_unknown_trades_read_does_not_clear_halt(tmp_path, meta):
    rm, store = _rm(tmp_path)
    rm.reset_day()
    store.apply_fill(Fill(meta.yes.token_id, Side.BUY, 0.5, 2.0, "t1"))
    rm.note_fill(Fill(meta.yes.token_id, Side.BUY, 0.5, 2.0, "t1"))
    rm.update_mark(meta.yes.token_id, 0.5)
    rm.reset_day()
    rm.update_mark(meta.yes.token_id, 0.01)
    assert rm.global_halt()[0] is True
    cleared, why = rm.try_clear_false_daily_loss(authoritative_ok=False)
    assert cleared is False and why == "authoritative_data_unknown"
    assert rm.daily_loss_latched is True
    store.close()


def test_authoritative_ok_clears_only_when_pnl_above_kill(tmp_path, meta):
    rm, store = _rm(tmp_path)
    tok = meta.yes.token_id
    store.set_position(tok, 9.85, 0.05)
    rm.update_mark(tok, 0.05)
    rm.reset_day()
    deltas = store.reconcile_positions({}, is_snapshot=True)
    for token_id, old_s, new_s, unit in deltas:
        rm.note_unexplained_inventory_delta((new_s - old_s) * 0.05)
    # Conservation kept daily_pnl ~0; latch was never tripped
    assert rm.daily_loss_latched is False
    # Manually latch then clear after authority + healthy pnl
    rm._daily_loss_latched = True
    cleared, why = rm.try_clear_false_daily_loss(authoritative_ok=True)
    assert cleared is True and why == "cleared_false_positive"
    assert rm.daily_loss_latched is False
    store.close()


def test_fills_from_rest_trade_flat_and_maker_orders():
    our = "0xabc"
    def other(t: str) -> str | None:
        return "no" if t == "yes" else "yes"

    flat = {
        "id": "t-flat",
        "asset_id": "yes",
        "side": "BUY",
        "size": "10",
        "price": "0.05",
        "status": "TRADE_STATUS_CONFIRMED",
        "trader_side": "MAKER",
        "maker_address": our,
        "match_time": "1700000000",
    }
    fills = fills_from_rest_trade(flat, our, other)
    assert len(fills) == 1
    assert fills[0].side is Side.SELL
    assert fills[0].size == 10

    nested = {
        "id": "t-nested",
        "asset_id": "yes",
        "side": "BUY",
        "outcome": "Yes",
        "status": "CONFIRMED",
        "timestamp": "1700000000000",
        "maker_orders": [
            {"maker_address": our, "matched_amount": "3", "price": "0.04", "outcome": "Yes"}
        ],
    }
    fills2 = fills_from_rest_trade(nested, our, other)
    assert len(fills2) == 1 and fills2[0].size == 3


def test_transient_net_error_detection() -> None:
    assert is_transient_net_error(TimeoutError("handshake timed out"))
    assert is_transient_net_error(RuntimeError("SSL: UNEXPECTED_EOF_WHILE_READING"))
    assert not is_transient_net_error(ValueError("bad payload"))


async def test_disconnect_recovery_blocks_quotes_until_authority(tmp_path, meta) -> None:
    eng = _engine_with_market(tmp_path, meta)
    eng._user_started = True
    eng._trades_backfill_ok = False
    eng._recovery_blocked = True
    eng._positions_healthy = False

    class Boom(StubGateway):
        async def get_trades(self, **kwargs):
            raise TradesReadError("TLS handshake timeout")

        async def positions(self):
            raise PositionsReadError("SSL timeout")

        async def open_orders(self):
            raise OpenOrdersReadError("down")

    eng.gateway = Boom()  # type: ignore[assignment]
    ok = await eng._authoritative_resync(reason="test")
    assert ok is False
    assert eng._recovery_blocked is True
    # risk halt not auto-cleared
    eng.risk._daily_loss_latched = True
    cleared, why = eng.risk.try_clear_false_daily_loss(authoritative_ok=False)
    assert not cleared and why == "authoritative_data_unknown"

    class Ok(StubGateway):
        async def get_trades(self, **kwargs):
            return []

        async def positions(self):
            return {}

        async def open_orders(self):
            return []

    eng.gateway = Ok()  # type: ignore[assignment]
    eng.risk._daily_loss_latched = True
    # With conservation-neutral flat book, daily_pnl may still be latched from
    # day_start; bootstrap a healthy baseline first.
    eng.risk._day_start_equity = eng.risk.equity
    eng.risk._persist_day()
    ok2 = await eng._authoritative_resync(reason="test_ok")
    assert ok2 is True
    assert eng._recovery_blocked is False
    assert eng._trades_backfill_ok is True
    eng.state.close()
    eng.catalog.close()


async def test_engine_reconcile_conserves_flatten(tmp_path, meta) -> None:
    eng = _engine_with_market(tmp_path, meta)
    eng.cfg.engine.reconcile_interval_s = 0.05
    tok = meta.yes.token_id
    eng.state.set_position(tok, 9.85, 0.05)
    eng.risk.update_mark(tok, 0.05)
    eng.risk.reset_day()
    start_eq = eng.risk.equity

    class Flat(StubGateway):
        async def positions(self):
            return {}

        async def open_orders(self):
            return []

        async def get_trades(self, **kwargs):
            return []

    eng.gateway = Flat()  # type: ignore[assignment]
    eng._running = True
    eng._trades_backfill_ok = True
    task = asyncio.create_task(eng._reconcile_loop())
    try:
        await asyncio.sleep(0.12)
        assert eng.state.position(tok).size == 0.0
        assert eng.state.fills_count() == 0
        assert eng.risk.net_cash == 0.0
        assert eng.risk.equity == pytest.approx(start_eq, abs=1e-6)
        assert eng.risk.daily_pnl == pytest.approx(0.0, abs=1e-6)
        assert eng.risk.global_halt()[0] is False
    finally:
        eng._running = False
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        eng.state.close()
        eng.catalog.close()
