"""Auto market selection: score, feasibility, hysteresis, allocation caps."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from polymaker.catalog.selection import (
    SelectCandidate,
    market_entry_feasible,
    score_market_for_selection,
    select_and_allocate,
)
from polymaker.config import Config
from polymaker.domain import MarketMeta, TokenMeta
from polymaker.strategy.budget import BudgetSliceInput, allocate_buy_budgets

NOW = datetime(2026, 11, 13, tzinfo=UTC).timestamp()


def _meta(
    cid: str,
    *,
    bid: float = 0.48,
    ask: float = 0.50,
    reward: float = 40.0,
    liquidity: float = 4000.0,
    hours: float = 24.0 * 30.0,
    min_size: float = 5.0,
    event: str = "evt",
) -> MarketMeta:
    end = (datetime.fromtimestamp(NOW, tz=UTC) + timedelta(hours=hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    return MarketMeta(
        condition_id=cid,
        question=cid,
        slug=cid,
        tokens=(TokenMeta(f"{cid}-yes", "Yes"), TokenMeta(f"{cid}-no", "No")),
        tick_size=0.01,
        neg_risk=False,
        min_order_size=min_size,
        rewards_min_size=5.0,
        rewards_max_spread=3.0,
        rewards_daily_rate=reward,
        maker_fee_bps=0,
        taker_fee_bps=0,
        fees_enabled=False,
        end_date_iso=end,
        event_id=event,
        best_bid=bid,
        best_ask=ask,
        liquidity_num=liquidity,
        volume_24hr=1000.0,
    )


def _cand(
    cid: str,
    score: float,
    *,
    event: str | None = None,
    minimum: float = 0.10,
    pinned: bool = False,
    inventory: bool = False,
    hours: float | None = 24.0 * 20.0,
    price_known: bool = True,
    filled: float = 0.0,
) -> SelectCandidate:
    return SelectCandidate(
        condition_id=cid,
        score=score,
        min_entry_notional=minimum,
        event_id=event or cid,
        pinned=pinned,
        has_inventory=inventory,
        filled_usdc=filled,
        hours_to_end=hours,
        slug=cid,
        price_known=price_known,
    )


def _plan(**kwargs: object):
    base: dict[str, object] = {
        "max_markets": 4,
        "available_usdc": 1.0,
        "fee_buffer_usdc": 0.01,
        "max_market_notional_usdc": 0.60,
        "max_event_group_loss_usdc": 0.95,
        "max_total_exposure_usdc": 1.0,
        "filled_total_usdc": 0.0,
        "min_hours_to_end": 6.0,
        "hysteresis_margin": 0.15,
    }
    base.update(kwargs)
    return select_and_allocate(**base)  # type: ignore[arg-type]


def test_score_rewards_spread_depth_and_time() -> None:
    base = _meta("base")
    more_reward = score_market_for_selection(_meta("r", reward=120.0), now=NOW)
    less_reward = score_market_for_selection(_meta("r0", reward=0.0), now=NOW)
    assert more_reward.reward > less_reward.reward
    assert more_reward.score > less_reward.score

    tight = score_market_for_selection(_meta("tight", bid=0.49, ask=0.50), now=NOW)
    wide = score_market_for_selection(_meta("wide", bid=0.40, ask=0.60), now=NOW)
    assert tight.spread > wide.spread
    assert tight.score > wide.score

    deep = score_market_for_selection(_meta("deep", liquidity=20000.0), now=NOW)
    thin = score_market_for_selection(_meta("thin", liquidity=10.0), now=NOW)
    assert deep.depth > thin.depth
    assert deep.score > thin.score

    far = score_market_for_selection(_meta("far", hours=24.0 * 40.0), now=NOW)
    soon = score_market_for_selection(_meta("soon", hours=10.0), now=NOW)
    assert far.time_to_resolution > soon.time_to_resolution
    assert far.score > soon.score
    assert base.condition_id == "base"


def test_feasibility_requires_min_size_times_price_inside_market_cap() -> None:
    cheap = _meta("cheap", bid=0.04, ask=0.06)  # cheapest 0.04 × 5 = 0.20
    pricey = _meta("pricey", bid=0.40, ask=0.42)  # cheapest 0.40 × 5 = 2.00
    near_end = _meta("soon", bid=0.04, ask=0.06, hours=2.0)
    caps = {
        "now": NOW,
        "max_market_notional_usdc": 0.60,
        "max_total_exposure_usdc": 1.0,
        "available_usdc": 1.0,
        "fee_buffer_usdc": 0.01,
        "min_hours_to_end": 6.0,
    }
    assert market_entry_feasible(cheap, **caps)
    assert not market_entry_feasible(pricey, **caps)
    assert not market_entry_feasible(near_end, **caps)
    # A tighter market cap rejects the cheap name once 5 × price no longer fits.
    assert not market_entry_feasible(cheap, **{**caps, "max_market_notional_usdc": 0.10})


def test_hysteresis_keeps_incumbent_until_challenger_clears_margin() -> None:
    hold = _plan(
        candidates=[_cand("old", 1.0), _cand("new", 1.10)],
        previous_ids=["old"],
        max_markets=1,
    )
    assert [p.condition_id for p in hold if p.role == "entry"] == ["old"]

    replaced = _plan(
        candidates=[_cand("old", 1.0), _cand("new", 1.20)],
        previous_ids=["old"],
        max_markets=1,
    )
    assert [p.condition_id for p in replaced if p.role == "entry"] == ["new"]


def test_inventory_is_retained_exit_only_when_not_an_entry() -> None:
    # Still feasible and held: a stronger name must not evict it.
    kept = _plan(
        candidates=[_cand("held", 1.0, inventory=True), _cand("new", 5.0)],
        previous_ids=["held"],
        max_markets=1,
    )
    assert [p.condition_id for p in kept if p.role == "entry"] == ["held"]

    # Infeasible price, but inventory remains as exit-only and does not take the slot.
    parked = _plan(
        candidates=[
            _cand("held", 0.1, inventory=True, minimum=5.0),
            _cand("new", 2.0, minimum=0.10),
        ],
        previous_ids=["held"],
        max_markets=1,
    )
    roles = {p.condition_id: p.role for p in parked}
    assert roles["held"] == "exit_only"
    assert roles["new"] == "entry"
    held = next(p for p in parked if p.condition_id == "held")
    assert held.buy_budget_usdc == 0.0


def test_pinned_market_is_kept_and_events_are_diversified() -> None:
    pinned = _plan(
        candidates=[_cand("listed", 0.01, pinned=True), _cand("best", 10.0)],
        max_markets=1,
    )
    assert [p.condition_id for p in pinned if p.role == "entry"] == ["listed"]

    diversified = _plan(
        candidates=[
            _cand("a", 10.0, event="e1"),
            _cand("b", 9.0, event="e1"),
            _cand("c", 3.0, event="e2"),
        ],
        max_markets=2,
    )
    ids = [p.condition_id for p in diversified if p.role == "entry"]
    assert ids[0] == "a"
    assert "c" in ids
    assert "b" not in ids


def test_config_loads_selection_switch(tmp_path) -> None:
    (tmp_path / "config.toml").write_text(
        "[selection]\nauto = true\nmax_markets = 3\nprofile = \"micro-tight\"\n",
        encoding="utf-8",
    )
    (tmp_path / "strategy.toml").write_text("", encoding="utf-8")
    (tmp_path / "markets.toml").write_text("", encoding="utf-8")
    cfg = Config.load(tmp_path, load_env=False)
    assert cfg.selection.auto is True
    assert cfg.selection.max_markets == 3
    assert cfg.selection.profile == "micro-tight"
    # Absent section stays off so the $20–30 template does not rotate.
    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / "config.toml").write_text("[risk]\nmax_total_exposure_usdc = 25\n", encoding="utf-8")
    (bare / "strategy.toml").write_text("", encoding="utf-8")
    (bare / "markets.toml").write_text("", encoding="utf-8")
    assert Config.load(bare, load_env=False).selection.auto is False


def test_minimum_above_quarter_stays_with_a_cheap_market() -> None:
    # Newsom 5 * 0.094 = $0.47. That is more than a $0.25 clip and more than
    # an equal half of a two-name book, and the cheap name still gets a bid.
    planned = _plan(
        candidates=[
            _cand("iran", 2.0, minimum=0.08, pinned=True),
            _cand("newsom", 3.0, minimum=0.47),
        ],
        max_markets=2,
        max_market_notional_usdc=1.0,
    )
    entries = [p for p in planned if p.role == "entry"]
    ids = {p.condition_id for p in entries}
    assert ids == {"iran", "newsom"}
    newsom = next(p for p in entries if p.condition_id == "newsom")
    assert newsom.scale_to_budget
    assert newsom.buy_budget_usdc > 0.25
    assert newsom.buy_budget_usdc + 1e-9 >= 0.47
    assert sum(p.buy_budget_usdc for p in entries) > 0.90


def test_cheap_minimums_can_rest_together() -> None:
    planned = _plan(
        candidates=[
            _cand("a", 3.0, minimum=0.08),
            _cand("b", 2.0, minimum=0.08),
            _cand("c", 1.0, minimum=0.08),
        ],
        max_markets=3,
    )
    entries = [p for p in planned if p.role == "entry"]
    assert [p.condition_id for p in entries] == ["a", "b", "c"]
    assert sum(p.buy_budget_usdc for p in entries) <= 0.99 + 1e-9
    assert sum(p.buy_budget_usdc for p in entries) > 0.90
    # Leftover cash is spread, so each slice is more than the $0.08 minimum.
    assert all(p.buy_budget_usdc + 1e-9 >= 0.16 for p in entries)


def test_fresh_name_does_not_evict_a_resting_incumbent() -> None:
    # Both minimums cannot share the dollar. The resting name keeps its slice.
    planned = _plan(
        candidates=[
            _cand("keep", 1.0, minimum=0.70),
            _cand("fresh", 1.2, minimum=0.40),
        ],
        previous_ids=["keep"],
        max_markets=2,
        max_market_notional_usdc=1.0,
    )
    ids = [p.condition_id for p in planned if p.role == "entry"]
    assert ids == ["keep"]
    keep = next(p for p in planned if p.condition_id == "keep")
    assert keep.scale_to_budget
    assert keep.buy_budget_usdc > 0.25


def test_allocation_spreads_and_respects_caps() -> None:
    three = _plan(
        candidates=[_cand("a", 3.0), _cand("b", 2.0), _cand("c", 1.0)],
        max_markets=3,
    )
    entries = [p for p in three if p.role == "entry"]
    assert len(entries) == 3
    amounts = [p.buy_budget_usdc for p in entries]
    pool = 0.99
    assert sum(amounts) <= pool + 1e-9
    assert max(amounts) <= pool / 3 + 1e-9
    assert max(amounts) < 0.50  # not the whole dollar, and not one market's cap
    assert min(amounts) >= 0.10 - 1e-9
    assert all(p.scale_to_budget for p in entries)

    lone = allocate_buy_budgets(
        [BudgetSliceInput("only", "e", min_entry_notional=0.10)],
        available_usdc=1.0,
        fee_buffer_usdc=0.01,
        max_market_notional_usdc=0.60,
        max_event_group_loss_usdc=0.95,
        max_total_exposure_usdc=1.0,
        filled_total_usdc=0.0,
        lone_market_fraction=0.60,
    )
    assert len(lone) == 1
    # Market cap binds. The old 60% soft clip of the $0.99 pool does not.
    assert abs(lone[0].buy_budget_usdc - 0.60) < 1e-9

    # A lone name uses the pool, not just its 5-share minimum.
    raised = allocate_buy_budgets(
        [BudgetSliceInput("only", "e", min_entry_notional=0.75)],
        available_usdc=1.0,
        fee_buffer_usdc=0.01,
        max_market_notional_usdc=1.0,
        max_event_group_loss_usdc=1.0,
        max_total_exposure_usdc=1.0,
        filled_total_usdc=0.0,
    )
    assert abs(raised[0].buy_budget_usdc - 0.99) < 1e-9
    assert raised[0].fits_minimum
    assert raised[0].buy_budget_usdc > 0.25

    same_event = allocate_buy_budgets(
        [
            BudgetSliceInput("a", "same", min_entry_notional=0.10),
            BudgetSliceInput("b", "same", min_entry_notional=0.10),
        ],
        available_usdc=1.0,
        fee_buffer_usdc=0.01,
        max_market_notional_usdc=0.60,
        max_event_group_loss_usdc=0.30,
        max_total_exposure_usdc=1.0,
        filled_total_usdc=0.0,
    )
    assert sum(s.buy_budget_usdc for s in same_event) <= 0.30 + 1e-9
    assert all(s.buy_budget_usdc <= 0.15 + 1e-9 for s in same_event)

    # Filled inventory consumes total room; resting elsewhere consumes cash.
    capped = allocate_buy_budgets(
        [
            BudgetSliceInput("a", "ea", min_entry_notional=0.10, filled_usdc=0.0),
            BudgetSliceInput("b", "eb", min_entry_notional=0.10, filled_usdc=0.0),
        ],
        available_usdc=1.0,
        fee_buffer_usdc=0.01,
        max_market_notional_usdc=0.60,
        max_event_group_loss_usdc=0.95,
        max_total_exposure_usdc=1.0,
        filled_total_usdc=0.70,
        resting_buy_elsewhere_usdc=0.10,
    )
    # cash 0.89, total room 0.30 → pool 0.30, fair 0.15
    assert sum(s.buy_budget_usdc for s in capped) <= 0.30 + 1e-9
    assert all(s.fits_minimum for s in capped)
