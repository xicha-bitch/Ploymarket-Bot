"""Integration test: one full engine recompute cycle with StubGateway (no network)."""

from __future__ import annotations

import asyncio
import contextlib
import time

from polymaker.config import Config, PathsConfig, StrategyProfile
from polymaker.domain import OpenOrder, OrderState, Side
from polymaker.engine import Engine
from polymaker.strategy.regime import RegimeMachine
from tests.stub_gateway import StubGateway


def _engine_with_market(tmp_path, meta) -> Engine:
    cfg = Config(paths=PathsConfig(db=str(tmp_path / "state.db"),
                                   journal_dir=str(tmp_path / "j"),
                                   log_dir=str(tmp_path / "l")))
    cfg.engine.journal = False
    eng = Engine(cfg)
    eng.gateway = StubGateway()  # type: ignore[assignment]
    cid = meta.condition_id
    # inject one market directly, bypassing network resolution
    eng.metas[cid] = meta
    eng.profiles[cid] = StrategyProfile()
    eng.est[cid] = Engine._make_estimators(eng.profiles[cid])
    eng.regime_m[cid] = RegimeMachine()
    eng._dirty[cid] = asyncio.Event()
    eng._locks[cid] = asyncio.Lock()
    for tok in (meta.yes.token_id, meta.no.token_id):
        eng._token_cid[tok] = cid
    eng.md.set_markets([(cid, [meta.yes.token_id, meta.no.token_id])])
    eng._running = True
    return eng


def _feed_book(eng, meta):
    now = time.time()  # fresh ts so the ws_stale guard doesn't HALT the market
    yb = eng.md.book(meta.yes.token_id)
    yb.apply_snapshot(bids=[(0.48, 500), (0.49, 500)], asks=[(0.51, 500), (0.52, 500)], ts=now)
    nb = eng.md.book(meta.no.token_id)
    nb.apply_snapshot(bids=[(0.48, 500), (0.49, 500)], asks=[(0.51, 500), (0.52, 500)], ts=now)
    eng.md.ack_snapshot(meta.yes.token_id)
    eng.md.ack_snapshot(meta.no.token_id)


async def test_recompute_places_two_sided_quotes(tmp_path, meta):
    eng = _engine_with_market(tmp_path, meta)
    _feed_book(eng, meta)
    await eng._recompute(meta.condition_id)

    yes_orders = eng.state.orders_for(meta.yes.token_id)
    no_orders = eng.state.orders_for(meta.no.token_id)
    assert yes_orders, "no YES quotes placed"
    assert no_orders, "no NO quotes placed"
    # entry quotes are BUYs on both tokens (the canonical two-sided quote)
    assert all(o.side is Side.BUY for o in yes_orders)
    assert all(o.side is Side.BUY for o in no_orders)
    eng.state.close()
    eng.catalog.close()


async def test_recompute_is_idempotent_within_tolerance(tmp_path, meta):
    eng = _engine_with_market(tmp_path, meta)
    _feed_book(eng, meta)
    await eng._recompute(meta.condition_id)
    n_after_first = len(eng.state.orders)
    # same book -> reconcile should be a no-op, order count unchanged
    await eng._recompute(meta.condition_id)
    assert len(eng.state.orders) == n_after_first
    eng.state.close()
    eng.catalog.close()


async def test_recompute_skips_when_book_empty(tmp_path, meta):
    eng = _engine_with_market(tmp_path, meta)
    # no book fed
    await eng._recompute(meta.condition_id)
    assert len(eng.state.orders) == 0
    eng.state.close()
    eng.catalog.close()


async def test_startup_reconcile_fails_closed_when_open_orders_unavailable(tmp_path, meta):
    import pytest
    from polymaker.domain import OpenOrder, OrderState, Side

    class Boom(StubGateway):
        async def open_orders(self):
            raise RuntimeError("net down")

    eng = _engine_with_market(tmp_path, meta)
    eng.gateway = Boom()  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="startup_open_orders_failed"):
        await eng._startup_reconcile()
    eng.state.close()
    eng.catalog.close()


async def test_startup_reconcile_fails_closed_when_orders_stuck(tmp_path, meta):
    import pytest
    from polymaker.domain import OpenOrder, OrderState, Side

    stuck = OpenOrder("leftover-1", meta.yes.token_id, Side.BUY, 0.4, 5.0, OrderState.LIVE)

    class Stuck(StubGateway):
        async def open_orders(self):
            return [stuck]

    eng = _engine_with_market(tmp_path, meta)
    eng.gateway = Stuck()  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="startup_orders_stuck"):
        await eng._startup_reconcile()
    eng.state.close()
    eng.catalog.close()


async def test_startup_reconcile_empty_snapshot_ok(tmp_path, meta):
    eng = _engine_with_market(tmp_path, meta)
    await eng._startup_reconcile()
    assert eng.state.position(meta.yes.token_id).size == 0
    eng.state.close()
    eng.catalog.close()


async def test_supervise_restarts_with_backoff_then_resets(tmp_path, meta):
    """A dead task is restarted after backoff; a later healthy interval resets it."""
    eng = _engine_with_market(tmp_path, meta)
    eng._supervise_interval_s = 0.02
    n = {"calls": 0}

    async def flaky() -> None:
        n["calls"] += 1
        if n["calls"] == 1:
            raise RuntimeError("boom")
        await asyncio.Event().wait()

    eng._spawn("flaky", flaky)
    sup = asyncio.create_task(eng._supervise())
    try:
        # first poll + default 1s backoff then respawn
        await asyncio.sleep(1.20)
        assert n["calls"] >= 2
        assert not eng._tasks["flaky"].done()
        # a healthy interval should have already cleared backoff
        assert "flaky" not in eng._restart_backoff_s
        # if backoff stayed at 2s, the next death would not respawn within ~1.2s
        eng._tasks["flaky"].cancel()
        await asyncio.sleep(1.25)
        assert n["calls"] >= 3
        assert not eng._tasks["flaky"].done()
    finally:
        eng._running = False
        sup.cancel()
        for task in list(eng._tasks.values()):
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sup
        eng.state.close()
        eng.catalog.close()


async def test_exit_only_market_sells_inventory_and_skips_buys(tmp_path, meta):
    eng = _engine_with_market(tmp_path, meta)
    _feed_book(eng, meta)
    eng.state.set_position(meta.yes.token_id, 10.0, 0.40)
    eng._exit_only.add(meta.condition_id)
    await eng._recompute(meta.condition_id)
    orders = eng.state.orders_for(meta.yes.token_id) + eng.state.orders_for(meta.no.token_id)
    assert orders, "exit quote missing"
    assert all(o.side is Side.SELL for o in orders)
    eng.state.close()
    eng.catalog.close()


def test_allocated_headroom_caps_one_market(tmp_path, meta):
    eng = _engine_with_market(tmp_path, meta)
    eng.cfg.selection.auto = True
    eng._buy_budget[meta.condition_id] = 0.15
    eng._collateral_usdc = 1.0
    headroom = eng._entry_headroom(meta)
    assert headroom is not None
    assert headroom <= 0.15 + 1e-9
    eng.state.close()
    eng.catalog.close()


def test_entry_headroom_keeps_a_live_bid_in_the_slice(tmp_path, meta):
    """API balance already reserved the bid; the target must still include it."""
    eng = _engine_with_market(tmp_path, meta)
    eng.cfg.selection.auto = True
    eng._buy_budget[meta.condition_id] = 0.50
    eng._collateral_usdc = 0.46
    eng.state.upsert_order(OpenOrder("oid", meta.yes.token_id, Side.BUY, 0.094, 5.0, OrderState.LIVE))
    headroom = eng._entry_headroom(meta)
    assert headroom is not None
    assert headroom + 1e-9 >= 0.47
    eng.state.close()
    eng.catalog.close()
