"""Regression: micro-capital budget gates vs rewards soft target / exchange min."""

from __future__ import annotations

from dataclasses import replace

import pytest

from polymaker.domain import Position, Quote, Regime, Side
from polymaker.strategy.budget import (
    BudgetSliceInput,
    allocate_buy_budgets,
    filter_quotes_to_budget,
    place_headroom_usdc,
    quote_buy_notional,
    resting_quote_headroom,
)
from polymaker.strategy.maker_gates import apply_min_size_clamp, is_min_size_feasible
from polymaker.strategy.quoting import QuoteInputs, construct_quotes, notional_covering_minimum
from tests.conftest import view


def test_apply_min_size_clamp_thin_uses_exchange_min_only(meta, profile):
    fat = replace(meta, rewards_min_size=200.0, min_order_size=5.0)
    # raw dust that previously got inflated to 200
    out = apply_min_size_clamp(5.0, scale=0.0, meta=fat, profile=profile)
    assert out == 5.0


def test_apply_min_size_clamp_deep_unchanged(meta, profile):
    out = apply_min_size_clamp(12.5, scale=1.0, meta=meta, profile=profile)
    assert out == 12.5


def test_place_headroom_subtracts_resting_and_caps():
    # available 1.0, resting 0.2, fee 0.01 → bal headroom 0.79
    # market cap room 0.55-0.1=0.45; total room 1.0-0.1=0.9 → min = 0.45
    h = place_headroom_usdc(
        available_usdc=1.0,
        resting_buy_usdc=0.2,
        filled_market_usdc=0.1,
        filled_total_usdc=0.1,
        max_market_notional_usdc=0.55,
        max_total_exposure_usdc=1.0,
        fee_buffer_usdc=0.01,
    )
    assert abs(h - 0.45) < 1e-9


def test_filter_keeps_affordable_leg_drops_expensive():
    # YES @ 0.05 × 5 = 0.25 fits; NO @ 0.95 × 5 = 4.75 does not (live failure shape)
    yes = Quote("yes-token", Side.BUY, 0.05, 5.0)
    no = Quote("no-token", Side.BUY, 0.95, 5.0)
    sell = Quote("yes-token", Side.SELL, 0.10, 5.0)
    d = filter_quotes_to_budget([yes, no, sell], headroom_usdc=1.0)
    assert sell in d.kept
    assert yes in d.kept
    assert no not in d.kept
    assert any("budget_reject" in s and "0.9500" in s for s in d.skipped)


def test_filter_never_splits_below_requested_size():
    q = Quote("yes-token", Side.BUY, 0.50, 5.0)  # needs 2.5; headroom 1.0
    d = filter_quotes_to_budget([q], headroom_usdc=1.0)
    assert d.kept == ()
    assert d.skipped and "budget_reject" in d.skipped[0]


def test_construct_quotes_thin_plus_budget_fits_micro_wallet(meta, profile):
    """End-to-end: rewards=200 thin path + $1 headroom → only affordable BUYs."""
    fat = replace(meta, rewards_min_size=200.0, min_order_size=5.0)
    tq = construct_quotes(
        QuoteInputs(
            meta=fat,
            regime=Regime.QUIET,
            fv=0.05,
            vol_short=0.0,
            toxicity=0.0,
            yes_view=view(0.04, 0.06, 5, 5),
            no_view=view(0.94, 0.96, 5, 5),
            pos_yes=Position("yes-token"),
            pos_no=Position("no-token"),
            profile=profile,
            now=1.0,
        )
    )
    buys = [q for q in tq.quotes if q.side is Side.BUY]
    assert buys
    # thin → exchange min sizes, not 200
    assert all(q.size == 5.0 for q in buys)
    d = filter_quotes_to_budget(list(tq.quotes), headroom_usdc=1.0)
    kept_buys = [q for q in d.kept if q.side is Side.BUY]
    assert kept_buys
    assert sum(quote_buy_notional(q) for q in kept_buys) <= 1.0 + 1e-9
    # expensive NO leg (if present) must be dropped
    for q in kept_buys:
        assert quote_buy_notional(q) <= 1.0 + 1e-9


def test_is_min_size_feasible():
    assert is_min_size_feasible(0.95, min_order_size=5.0, headroom_usdc=None) is True
    assert is_min_size_feasible(0.50, min_order_size=5.0, headroom_usdc=2.50) is True
    assert is_min_size_feasible(0.10, min_order_size=5.0, headroom_usdc=1.00) is True
    assert is_min_size_feasible(0.95, min_order_size=5.0, headroom_usdc=1.00) is False
    assert is_min_size_feasible(0.0, min_order_size=5.0, headroom_usdc=1.00) is True
    assert is_min_size_feasible(0.5, min_order_size=0.0, headroom_usdc=1.00) is True


def test_budget_tolerance_keeps_sub_cent_dust():
    # Live aliens reject: 10.02 * 0.015 = 0.1503 vs headroom 0.150289.
    dust = Quote("tok", Side.BUY, 0.015, 10.02)
    decision = filter_quotes_to_budget([dust], headroom_usdc=0.150289)
    assert dust in decision.kept


def test_budget_tolerance_still_rejects_a_real_shortfall():
    expensive = Quote("tok", Side.BUY, 0.20, 5.0)  # $1.00 vs $0.15
    decision = filter_quotes_to_budget([expensive], headroom_usdc=0.15)
    assert decision.kept == ()
    assert any("reason=infeasible" in s for s in decision.skipped)


def test_resting_quote_headroom_counts_the_live_bid():
    # API available already reserved the 0.47 bid, so incremental room is ~0.
    # The target must still see that bid or the next pass cancels it.
    assert resting_quote_headroom(
        incremental_headroom=0.0,
        own_resting_buy_usdc=0.47,
        buy_budget_usdc=0.50,
    ) == 0.47


def test_filter_distinguishes_infeasible_vs_exhausted():
    q1 = Quote("tok1", Side.BUY, 0.40, 5.0)  # cost 2.0
    q2 = Quote("tok2", Side.BUY, 0.40, 5.0)  # cost 2.0
    q3 = Quote("tok3", Side.BUY, 0.80, 5.0)  # cost 4.0
    d = filter_quotes_to_budget([q1, q2, q3], headroom_usdc=3.0)
    assert q1 in d.kept
    assert q2 not in d.kept
    assert q3 not in d.kept
    assert any("reason=exhausted" in s for s in d.skipped)
    assert any("reason=infeasible" in s for s in d.skipped)


def test_quote_uses_more_than_quarter_clip_when_minimum_needs_it(meta, profile):
    """5 shares at $0.10 cost $0.50. A $0.25 base must not drop that bid."""
    assert notional_covering_minimum(0.25, 0.10, 5.0, 1.0) == 0.50
    # Headroom that cannot pay the minimum stays on the clip; the leg is then skipped.
    assert notional_covering_minimum(0.25, 0.10, 5.0, 0.30) == 0.25

    tight = profile.with_overrides(
        {"base_size_usdc": 0.25, "layers": 1, "reward_size_mult": 1.0, "thin_depth_mult": 1.0}
    )
    # Deep book (scale 1). Without the lift, $0.25 / $0.10 = 2.5 shares is
    # below the 5-share minimum and the bid is dropped.
    fat = replace(meta, rewards_min_size=5.0, min_order_size=5.0)
    tq = construct_quotes(
        QuoteInputs(
            meta=fat,
            regime=Regime.QUIET,
            fv=0.12,
            vol_short=0.0,
            toxicity=0.0,
            yes_view=view(0.10, 0.12, 500, 500),
            no_view=view(0.86, 0.90, 500, 500),
            pos_yes=Position("yes-token"),
            pos_no=Position("no-token"),
            profile=tight,
            now=1.0,
            headroom_usdc=1.0,
        )
    )
    buys = [q for q in tq.quotes if q.side is Side.BUY]
    assert buys
    assert all(quote_buy_notional(q) > 0.25 for q in buys)
    yes = [q for q in buys if q.token_id == "yes-token"]
    assert yes
    assert yes[0].size + 1e-9 >= 5.0
    assert quote_buy_notional(yes[0]) == pytest.approx(0.50, abs=1e-6)


def test_slice_above_quarter_still_funds_cheap_neighbors():
    slices = allocate_buy_budgets(
        [
            BudgetSliceInput("rich", "e1", min_entry_notional=0.40),
            BudgetSliceInput("cheap", "e2", min_entry_notional=0.08),
            BudgetSliceInput("cheap2", "e3", min_entry_notional=0.08),
        ],
        available_usdc=1.0,
        fee_buffer_usdc=0.01,
        max_market_notional_usdc=1.0,
        max_event_group_loss_usdc=0.95,
        max_total_exposure_usdc=1.0,
        filled_total_usdc=0.0,
    )
    by_id = {s.condition_id: s for s in slices}
    assert by_id["rich"].fits_minimum
    assert by_id["rich"].buy_budget_usdc > 0.25
    assert by_id["rich"].buy_budget_usdc + 1e-9 >= 0.40
    assert by_id["cheap"].fits_minimum
    assert by_id["cheap2"].fits_minimum
    assert sum(s.buy_budget_usdc for s in slices) > 0.90


def test_same_event_minimum_can_exceed_equal_share():
    slices = allocate_buy_budgets(
        [
            BudgetSliceInput("rich", "same", min_entry_notional=0.60),
            BudgetSliceInput("cheap", "same", min_entry_notional=0.08),
        ],
        available_usdc=1.0,
        fee_buffer_usdc=0.01,
        max_market_notional_usdc=1.0,
        max_event_group_loss_usdc=0.95,
        max_total_exposure_usdc=1.0,
        filled_total_usdc=0.0,
    )
    by_id = {s.condition_id: s for s in slices}
    assert by_id["rich"].fits_minimum
    assert by_id["rich"].buy_budget_usdc > 0.25
    assert by_id["rich"].buy_budget_usdc + 1e-9 >= 0.60
    assert by_id["cheap"].fits_minimum
    assert sum(s.buy_budget_usdc for s in slices) <= 0.95 + 1e-9


def test_construct_quotes_skips_infeasible_buy_preserves_exit(meta, profile):
    """construct_quotes skips BUY leg when min size exceeds headroom, but retains exits."""
    fat = replace(meta, rewards_min_size=200.0, min_order_size=5.0)
    tq = construct_quotes(
        QuoteInputs(
            meta=fat,
            regime=Regime.QUIET,
            fv=0.05,
            vol_short=0.0,
            toxicity=0.0,
            yes_view=view(0.04, 0.06, 5, 5),
            no_view=view(0.94, 0.96, 5, 5),
            pos_yes=Position("yes-token"),
            pos_no=Position("no-token", size=10.0, avg_price=0.90),
            profile=profile,
            now=1.0,
            headroom_usdc=1.0,
        )
    )
    buys = [q for q in tq.quotes if q.side is Side.BUY]
    sells = [q for q in tq.quotes if q.side is Side.SELL]
    assert not any(q.token_id == "no-token" for q in buys)
    assert any(q.token_id == "no-token" for q in sells)
