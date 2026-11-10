"""Regression: micro-capital budget gates vs rewards soft target / exchange min."""

from __future__ import annotations

from dataclasses import replace

from polymaker.domain import Quote, Side
from polymaker.strategy.budget import (
    filter_quotes_to_budget,
    place_headroom_usdc,
    quote_buy_notional,
)
from polymaker.strategy.maker_gates import apply_min_size_clamp
from polymaker.strategy.quoting import QuoteInputs, construct_quotes
from tests.conftest import view
from polymaker.domain import Position, Regime


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
