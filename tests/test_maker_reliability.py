"""Unit tests for maker reliability gates (LIVE-ONLY engineering stubs).

These are offline pure-function / state-machine checks — not a paper product mode.
"""

from __future__ import annotations

from polymaker.domain import OpenOrder, OrderState, Position, Quote, Regime, Side
from polymaker.execution.batch import chunk_quotes
from polymaker.execution.intent import (
    IntentState,
    PlacementIntent,
    PlacementIntentStore,
    client_key_for,
)
from polymaker.execution.reconciler import reconcile
from polymaker.risk.manager import RiskManager
from polymaker.state.store import StateStore
from polymaker.state.tracker import OrderEvent, UserEventProcessor
from polymaker.strategy.maker_gates import (
    compute_exit_urgency,
    pair_edge_ok,
    should_min_size_only,
    size_scale_for_book,
)
from polymaker.strategy.quoting import QuoteInputs, construct_quotes
from tests.conftest import view


def test_compute_exit_urgency_hold_and_adverse():
    assert compute_exit_urgency(held_s=0, adverse=0, exit_urgency_s=600) == 0.0
    assert compute_exit_urgency(held_s=600, adverse=0, exit_urgency_s=600) == 1.0
    mid = compute_exit_urgency(held_s=300, adverse=0, exit_urgency_s=600)
    assert 0.4 <= mid <= 0.6
    hot = compute_exit_urgency(held_s=60, adverse=0.03, exit_urgency_s=600)
    assert hot > mid


def test_size_scale_thin_and_gappy_books(profile):
    deep = view(0.49, 0.51, bb_sz=500, ba_sz=500)
    assert size_scale_for_book(deep, tick=0.01, rewards_min_size=10, profile=profile) == 1.0
    thin = view(0.49, 0.51, bb_sz=5, ba_sz=5)
    assert size_scale_for_book(thin, tick=0.01, rewards_min_size=10, profile=profile) == 0.0
    # air pocket: best 0.49, second 0.40 → 9 ticks >= default air_gap_ticks=8
    gappy = view(0.49, 0.51, bb_sz=500, ba_sz=500)
    from polymaker.marketdata.orderbook import BookView

    gappy = BookView(
        best_bid=0.49,
        best_bid_size=500,
        best_ask=0.51,
        best_ask_size=500,
        second_bid=0.40,
        second_ask=0.52,
        bid_depth=500,
        ask_depth=500,
    )
    assert size_scale_for_book(gappy, tick=0.01, rewards_min_size=10, profile=profile) == 0.0


def test_pair_edge_gate():
    assert pair_edge_ok(0.48, 0.48, tick=0.01, min_edge_ticks=1)
    assert not pair_edge_ok(0.50, 0.50, tick=0.01, min_edge_ticks=1)


def test_should_min_size_only_connected_thin_silent(profile):
    assert not should_min_size_only(
        connected=True, book_age_s=10, depth=500, rewards_min_size=10, profile=profile
    )
    assert should_min_size_only(
        connected=True, book_age_s=200, depth=5, rewards_min_size=10, profile=profile
    )
    assert not should_min_size_only(
        connected=False, book_age_s=200, depth=5, rewards_min_size=10, profile=profile
    )


def test_thin_book_clamps_quote_size(meta, profile):
    deep = construct_quotes(
        QuoteInputs(
            meta=meta,
            regime=Regime.QUIET,
            fv=0.50,
            vol_short=0.0,
            toxicity=0.0,
            yes_view=view(0.49, 0.51, 500, 500),
            no_view=view(0.49, 0.51, 500, 500),
            pos_yes=Position("yes-token"),
            pos_no=Position("no-token"),
            profile=profile,
            now=1.0,
        )
    )
    thin = construct_quotes(
        QuoteInputs(
            meta=meta,
            regime=Regime.QUIET,
            fv=0.50,
            vol_short=0.0,
            toxicity=0.0,
            yes_view=view(0.49, 0.51, 5, 5),
            no_view=view(0.49, 0.51, 5, 5),
            pos_yes=Position("yes-token"),
            pos_no=Position("no-token"),
            profile=profile,
            now=1.0,
        )
    )
    deep_sz = max(q.size for q in deep.quotes if q.side is Side.BUY)
    thin_sz = max(q.size for q in thin.quotes if q.side is Side.BUY)
    floor = meta.rewards_min_size * profile.reward_size_mult
    assert thin_sz <= floor + 1e-9
    assert deep_sz >= thin_sz


def test_chunk_quotes_respects_batch_cap():
    qs = [Quote("t", Side.BUY, 0.4, 10.0) for _ in range(12)]
    batches = chunk_quotes(qs, 5)
    assert [len(b) for b in batches] == [5, 5, 2]


def test_placement_intent_partial_acks_need_quarantine():
    store = PlacementIntentStore()
    key = client_key_for("0xcond", [("yes", "BUY", 0.4, 10.0), ("no", "BUY", 0.4, 10.0)])
    store.record(PlacementIntent(client_key=key, condition_id="0xcond", n_quotes=2))
    store.ack(key, ["oid-1"])  # partial
    assert store.should_quarantine(key)
    assert store.get(key).state in (IntentState.UNKNOWN, IntentState.QUARANTINED)


def test_placement_intent_full_ack_clears_quarantine_flag():
    store = PlacementIntentStore()
    key = "k1"
    store.record(PlacementIntent(client_key=key, condition_id="0xcond", n_quotes=2))
    store.ack(key, ["a", "b"])
    assert store.get(key).state is IntentState.ACKED
    assert not store.should_quarantine(key)


def test_partial_fill_sets_order_state(tmp_path):
    s = StateStore(tmp_path / "s.db")
    p = UserEventProcessor(s)
    p.on_order(OrderEvent("o1", "tok", Side.BUY, 0.49, 100), "cid")
    assert s.orders_for("tok")[0].state is OrderState.LIVE
    p.on_order(OrderEvent("o1", "tok", Side.BUY, 0.49, 40), "cid")
    assert s.orders_for("tok")[0].state is OrderState.PARTIALLY_FILLED
    assert s.orders_for("tok")[0].size == 40
    s.close()


def test_reconcile_resizes_partially_filled_order():
    from polymaker.domain import TargetQuotes

    tq = TargetQuotes("cid", Regime.QUIET, (Quote("tok", Side.BUY, 0.49, 100),))
    live = [
        OpenOrder("o1", "tok", Side.BUY, 0.49, 40, OrderState.PARTIALLY_FILLED)
    ]
    plan = reconcile(tq, live, tick=0.01, reprice_ticks=2, resize_frac=0.15)
    assert plan.to_cancel == ["o1"]
    assert len(plan.to_place) == 1


def test_worst_case_exposure_advisory(tmp_path, meta):
    s = StateStore(tmp_path / "s.db")
    from polymaker.config import RiskConfig

    rm = RiskManager(RiskConfig(max_market_notional_usdc=12.0), s)
    rm.update_mark(meta.yes.token_id, 0.5)
    rm.update_mark(meta.no.token_id, 0.5)
    s.upsert_order(OpenOrder("o1", meta.yes.token_id, Side.BUY, 0.5, 10, OrderState.LIVE))
    # filled 0 + resting 5 notional (< 70% of 12) → advisory scale 1.0
    assert rm.worst_case_market_notional(meta) == 5.0
    assert rm.advisory_size_scale(meta) == 1.0
    s.upsert_order(OpenOrder("o2", meta.no.token_id, Side.BUY, 0.5, 20, OrderState.LIVE))
    # 15 notional > 12 → headroom 0
    assert rm.worst_case_market_notional(meta) == 15.0
    assert rm.advisory_size_scale(meta) == 0.0
    s.close()


def test_engine_wires_nonzero_exit_urgency_after_hold(tmp_path, meta, profile):
    """Regression: exit_urgency_s must not stay dead (always 0) in the engine."""
    from polymaker.config import Config, PathsConfig
    from polymaker.engine import Engine
    from tests.stub_gateway import StubGateway

    cfg = Config(
        paths=PathsConfig(
            db=str(tmp_path / "e.db"),
            journal_dir=str(tmp_path / "j"),
            log_dir=str(tmp_path / "l"),
        )
    )
    eng = Engine(cfg)
    eng.gateway = StubGateway()
    eng.metas[meta.condition_id] = meta
    eng.profiles[meta.condition_id] = profile.model_copy(update={"exit_urgency_s": 100.0})
    eng._inv_open_ts[meta.yes.token_id] = 1000.0
    u = eng._exit_urgency_for(
        meta.yes.token_id, 0.55, 0.50, eng.profiles[meta.condition_id], 1100.0
    )
    assert u > 0.5
