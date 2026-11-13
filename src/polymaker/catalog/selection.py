"""Auto-pick several markets and keep the set stable across refreshes.

Pure functions. The engine supplies Gamma/CLOB metadata and inventory; this
module does not place orders. Scoring reuses the catalog reward-density
signal and adds spread, depth, and time-to-resolution. Feasibility is a hard
filter: exchange min size × cheapest entry price must fit the per-market cap
(and the cash / total caps). Event ids are diversified before a second name
from the same event is taken. Hysteresis keeps an incumbent unless a
challenger beats it by a margin. A market that still holds inventory is never
dropped — it stays exit-only until flat.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from polymaker.catalog.scoring import extremity, reward_density
from polymaker.domain import MarketMeta
from polymaker.strategy.budget import (
    BudgetSlice,
    BudgetSliceInput,
    allocate_buy_budgets,
    slice_can_rearm,
)

_EPS = 1e-9


@dataclass(frozen=True, slots=True)
class SelectionScore:
    """Component scores in [0, 1] (except the combined ``score``)."""

    condition_id: str
    reward: float
    spread: float
    depth: float
    time_to_resolution: float
    extremity: float
    score: float
    hours_to_end: float | None
    min_entry_notional: float | None


@dataclass(frozen=True, slots=True)
class SelectCandidate:
    """One market the picker may keep, add, or hold for exit."""

    condition_id: str
    score: float
    min_entry_notional: float
    event_id: str | None = None
    pinned: bool = False
    has_inventory: bool = False
    filled_usdc: float = 0.0
    resting_buy_usdc: float = 0.0
    hours_to_end: float | None = None
    slug: str = ""
    price_known: bool = True


@dataclass(frozen=True, slots=True)
class PlannedMarket:
    """Selector output. ``exit_only`` markets are not given BUY budget."""

    condition_id: str
    role: str  # "entry" | "exit_only"
    buy_budget_usdc: float
    score: float
    slug: str = ""
    scale_to_budget: bool = False


def hours_to_resolution(end_date_iso: str | None, now: float) -> float | None:
    """Hours until ``end_date_iso``. Past or unparseable dates are unknown."""
    if not end_date_iso:
        return None
    try:
        dt = datetime.fromisoformat(end_date_iso.replace("Z", "+00:00"))
        hrs = (dt.timestamp() - now) / 3600.0
    except (ValueError, TypeError, OSError):
        return None
    return hrs if hrs > 0.0 else None


def cheapest_entry_price(meta: MarketMeta) -> float | None:
    """Lowest post-only BUY we might rest, from the YES top of book.

    YES joins the bid. NO is approximately ``1 - YES ask``. A locked or
    missing book returns None (not a guess at 0.5 — that would look cheap).
    """
    bid = meta.best_bid
    ask = meta.best_ask
    if bid <= 0.0 or ask <= 0.0 or ask >= 1.0 or bid >= ask:
        return None
    prices = [p for p in (bid, 1.0 - ask) if 0.0 < p < 1.0]
    if not prices:
        return None
    return min(prices)


def min_entry_notional(meta: MarketMeta) -> float | None:
    """USDC to post one exchange-minimum order at the cheapest entry."""
    price = cheapest_entry_price(meta)
    if price is None or meta.min_order_size <= 0.0:
        return None
    return price * meta.min_order_size


def _time_quality(hours: float | None, min_hours_to_end: float) -> float:
    if hours is None:
        return 0.55
    if hours < min_hours_to_end:
        return 0.0
    # Full mark once a week remains; ramp up from the entry floor.
    span = max(1.0, 24.0 * 7.0 - min_hours_to_end)
    return min(1.0, (hours - min_hours_to_end) / span)


def score_market_for_selection(
    meta: MarketMeta,
    *,
    now: float,
    min_hours_to_end: float = 6.0,
) -> SelectionScore:
    """Rank a market for a ~$1 maker book.

    Reward uses the existing reward-density helper at a $1 quote (not the
    $100 scanner default). Spread, depth, and time are each in ``[0, 1]``.
    Extremity is a light discount: cheap markets are the ones whose minimum
    size fits a micro cap, so a hard extremity ban would select nothing.
    """
    hours = hours_to_resolution(meta.end_date_iso, now)
    notional = min_entry_notional(meta)

    rate_norm = min(1.0, math.log1p(max(0.0, meta.rewards_daily_rate)) / math.log1p(200.0))
    density = reward_density(meta, quote_size_usdc=1.0)
    density_norm = min(1.0, density / 5.0)
    reward = min(1.0, 0.7 * rate_norm + 0.3 * density_norm)

    if meta.best_bid > 0.0 and meta.best_ask > meta.best_bid:
        spread = meta.best_ask - meta.best_bid
        spread_q = 1.0 / (1.0 + spread * 20.0)
    else:
        spread_q = 0.0

    depth_q = min(1.0, math.log1p(max(0.0, meta.liquidity_num)) / math.log1p(5000.0))
    time_q = _time_quality(hours, min_hours_to_end)
    ext = extremity(meta)

    combined = (
        0.40 * reward + 0.25 * spread_q + 0.20 * depth_q + 0.15 * time_q
    ) * (1.0 - 0.20 * ext)
    return SelectionScore(
        condition_id=meta.condition_id,
        reward=round(reward, 6),
        spread=round(spread_q, 6),
        depth=round(depth_q, 6),
        time_to_resolution=round(time_q, 6),
        extremity=round(ext, 6),
        score=round(max(0.0, combined), 6),
        hours_to_end=hours,
        min_entry_notional=None if notional is None else round(notional, 6),
    )


def market_entry_feasible(
    meta: MarketMeta,
    *,
    now: float,
    max_market_notional_usdc: float,
    max_total_exposure_usdc: float,
    available_usdc: float,
    fee_buffer_usdc: float = 0.0,
    min_hours_to_end: float = 6.0,
    pinned: bool = False,
    has_inventory: bool = False,
    filled_usdc: float = 0.0,
) -> bool:
    """True when a new BUY at exchange min fits the caps.

    Pinned names and names we already hold are not rejected here — the
    picker keeps them and the quoter enforces the live budget. This filter
    is for markets we would newly enter.
    """
    if pinned or has_inventory:
        return True
    notional = min_entry_notional(meta)
    hours = hours_to_resolution(meta.end_date_iso, now)
    return candidate_entry_feasible(
        SelectCandidate(
            condition_id=meta.condition_id,
            score=0.0,
            min_entry_notional=0.0 if notional is None else notional,
            hours_to_end=hours,
            price_known=notional is not None,
            filled_usdc=filled_usdc,
        ),
        max_market_notional_usdc=max_market_notional_usdc,
        max_total_exposure_usdc=max_total_exposure_usdc,
        available_usdc=available_usdc,
        fee_buffer_usdc=fee_buffer_usdc,
        min_hours_to_end=min_hours_to_end,
    )


def candidate_entry_feasible(
    cand: SelectCandidate,
    *,
    max_market_notional_usdc: float,
    max_total_exposure_usdc: float,
    available_usdc: float,
    fee_buffer_usdc: float,
    min_hours_to_end: float,
) -> bool:
    """Hard entry filter. Inventory retention is applied by the picker."""
    if not cand.price_known or cand.min_entry_notional <= _EPS:
        return False
    cash = max(0.0, available_usdc - max(0.0, fee_buffer_usdc))
    market_room = max_market_notional_usdc - max(0.0, cand.filled_usdc)
    if cand.min_entry_notional > market_room + _EPS:
        return False
    if cand.min_entry_notional > max_total_exposure_usdc + _EPS:
        return False
    if cand.min_entry_notional > cash + _EPS:
        return False
    return cand.hours_to_end is None or cand.hours_to_end >= min_hours_to_end


def select_and_allocate(
    candidates: list[SelectCandidate] | tuple[SelectCandidate, ...],
    *,
    max_markets: int,
    previous_ids: tuple[str, ...] | list[str] = (),
    hysteresis_margin: float = 0.15,
    available_usdc: float,
    fee_buffer_usdc: float,
    max_market_notional_usdc: float,
    max_event_group_loss_usdc: float,
    max_total_exposure_usdc: float,
    filled_total_usdc: float,
    resting_buy_elsewhere_usdc: float = 0.0,
    lone_market_fraction: float = 0.60,
    min_hours_to_end: float = 6.0,
) -> tuple[PlannedMarket, ...]:
    """Pick up to ``max_markets`` entry names plus any exit-only inventory."""
    cap_n = max(1, max_markets)
    feasible: list[SelectCandidate] = []
    for cand in candidates:
        if cand.pinned or candidate_entry_feasible(
            cand,
            max_market_notional_usdc=max_market_notional_usdc,
            max_total_exposure_usdc=max_total_exposure_usdc,
            available_usdc=available_usdc,
            fee_buffer_usdc=fee_buffer_usdc,
            min_hours_to_end=min_hours_to_end,
        ):
            feasible.append(cand)

    chosen = _prefer_entries(
        feasible,
        previous=set(previous_ids),
        margin=max(0.0, hysteresis_margin),
        max_markets=cap_n,
    )
    chosen, budgets = _fit_budgets(
        chosen,
        feasible,
        previous=set(previous_ids),
        max_markets=cap_n,
        available_usdc=available_usdc,
        fee_buffer_usdc=fee_buffer_usdc,
        max_market_notional_usdc=max_market_notional_usdc,
        max_event_group_loss_usdc=max_event_group_loss_usdc,
        max_total_exposure_usdc=max_total_exposure_usdc,
        filled_total_usdc=filled_total_usdc,
        resting_buy_elsewhere_usdc=resting_buy_elsewhere_usdc,
        lone_market_fraction=lone_market_fraction,
    )

    entry: list[SelectCandidate] = []
    for cand in chosen:
        sl = budgets.get(cand.condition_id)
        if cand.pinned:
            entry.append(cand)
            continue
        if sl is not None and sl.fits_minimum and sl.buy_budget_usdc > _EPS:
            entry.append(cand)
    positive = sum(
        1
        for cand in entry
        if (budgets.get(cand.condition_id) and budgets[cand.condition_id].buy_budget_usdc > _EPS)
    )
    scale = positive >= 2
    entry_ids = {c.condition_id for c in entry}

    planned: list[PlannedMarket] = []
    for cand in entry:
        sl = budgets.get(cand.condition_id)
        amount = 0.0 if sl is None else sl.buy_budget_usdc
        if sl is not None and not sl.fits_minimum and not cand.pinned:
            amount = 0.0
        if cand.pinned and sl is not None and not sl.fits_minimum:
            # Gamma minimum does not fit this share. Keep the subscription
            # but do not reserve cash the order cannot use.
            amount = 0.0
        planned.append(
            PlannedMarket(
                condition_id=cand.condition_id,
                role="entry",
                buy_budget_usdc=amount,
                score=cand.score,
                slug=cand.slug,
                scale_to_budget=scale and amount > _EPS,
            )
        )

    for cand in candidates:
        if cand.condition_id in entry_ids:
            continue
        if cand.has_inventory:
            planned.append(
                PlannedMarket(
                    condition_id=cand.condition_id,
                    role="exit_only",
                    buy_budget_usdc=0.0,
                    score=cand.score,
                    slug=cand.slug,
                    scale_to_budget=False,
                )
            )
    return tuple(planned)


def _prefer_entries(
    feasible: list[SelectCandidate],
    *,
    previous: set[str],
    margin: float,
    max_markets: int,
) -> list[SelectCandidate]:
    def _key(cand: SelectCandidate) -> tuple[float, str]:
        return (-cand.score, cand.condition_id)

    pinned = sorted([c for c in feasible if c.pinned], key=_key)
    incumbents = sorted(
        [c for c in feasible if c.condition_id in previous and not c.pinned],
        key=_key,
    )
    fresh = sorted(
        [c for c in feasible if c.condition_id not in previous and not c.pinned],
        key=_key,
    )

    selected: list[SelectCandidate] = []
    seen: set[str] = set()

    def add(cand: SelectCandidate) -> None:
        if cand.condition_id in seen:
            return
        selected.append(cand)
        seen.add(cand.condition_id)

    def room() -> int:
        return max_markets - len(selected)

    def same_event(cand: SelectCandidate) -> bool:
        return bool(cand.event_id) and any(s.event_id == cand.event_id for s in selected)

    for cand in pinned:
        add(cand)

    for allow_same in (False, True):
        for cand in incumbents:
            if room() <= 0:
                break
            if not allow_same and same_event(cand):
                continue
            add(cand)
    for allow_same in (False, True):
        for cand in fresh:
            if room() <= 0:
                break
            if not allow_same and same_event(cand):
                continue
            add(cand)

    for cand in fresh:
        if cand.condition_id in seen:
            continue
        if room() > 0:
            add(cand)
            continue
        victims = [
            s
            for s in selected
            if not s.pinned and not s.has_inventory and s.condition_id in previous
        ]
        if not victims:
            continue
        weakest = min(victims, key=lambda s: (s.score, s.condition_id))
        if cand.score <= weakest.score * (1.0 + margin) + _EPS:
            continue
        if cand.event_id and any(
            s.event_id == cand.event_id and s.condition_id != weakest.condition_id
            for s in selected
        ):
            continue
        selected = [s for s in selected if s.condition_id != weakest.condition_id]
        seen.discard(weakest.condition_id)
        add(cand)
    return selected


def _would_churn(cand: SelectCandidate, budgets: dict[str, BudgetSlice], group: list[SelectCandidate]) -> bool:
    """True when this auto name would post a minimum and then be cancelled.

    Pinned names stay. A market that is the only auto name and can at least
    fit its minimum is kept; price-stable retain holds that single order.
    """
    if cand.pinned:
        return False
    sl = budgets.get(cand.condition_id)
    if sl is None or slice_can_rearm(sl.buy_budget_usdc, cand.min_entry_notional):
        return False
    other_autos = any(not c.pinned and c.condition_id != cand.condition_id for c in group)
    pinned_present = any(c.pinned for c in group)
    alone = not other_autos and not pinned_present and sl.fits_minimum
    return not alone


def _fit_budgets(
    chosen: list[SelectCandidate],
    feasible: list[SelectCandidate],
    *,
    previous: set[str],
    max_markets: int,
    available_usdc: float,
    fee_buffer_usdc: float,
    max_market_notional_usdc: float,
    max_event_group_loss_usdc: float,
    max_total_exposure_usdc: float,
    filled_total_usdc: float,
    resting_buy_elsewhere_usdc: float,
    lone_market_fraction: float,
) -> tuple[list[SelectCandidate], dict[str, BudgetSlice]]:
    """Drop autos that cannot fund an exchange minimum; refill the free slots.

    A name whose minimum consumes its slice is dropped: the next quote pass
    would see leftover cash below one more 5-share order and cancel the bid
    it just posted. Fresh names are shed before incumbents, so a refresh does
    not churn a resting market to make room for a new one.
    """
    group = list(chosen)

    def alloc(group_now: list[SelectCandidate]) -> dict[str, BudgetSlice]:
        slices = allocate_buy_budgets(
            [
                BudgetSliceInput(
                    condition_id=c.condition_id,
                    event_id=c.event_id,
                    min_entry_notional=c.min_entry_notional if c.price_known else 0.0,
                    filled_usdc=c.filled_usdc,
                )
                for c in group_now
            ],
            available_usdc=available_usdc,
            fee_buffer_usdc=fee_buffer_usdc,
            max_market_notional_usdc=max_market_notional_usdc,
            max_event_group_loss_usdc=max_event_group_loss_usdc,
            max_total_exposure_usdc=max_total_exposure_usdc,
            filled_total_usdc=filled_total_usdc,
            resting_buy_elsewhere_usdc=resting_buy_elsewhere_usdc,
            lone_market_fraction=lone_market_fraction,
        )
        return {s.condition_id: s for s in slices}

    guard = 0
    while group and guard < 32:
        guard += 1
        budgets = alloc(group)
        broken = [
            c
            for c in group
            if not c.pinned and not budgets[c.condition_id].fits_minimum
        ]
        churn = [c for c in group if _would_churn(c, budgets, group)]
        if not broken and not churn:
            break
        # Shed a fresh name before an incumbent. Adding a new market must not
        # shrink a name we already quote until its own order would be cancelled.
        fresh = [c for c in group if not c.pinned and c.condition_id not in previous]
        if fresh and any(c.condition_id in previous for c in broken + churn):
            victim = min(fresh, key=lambda c: (c.score, c.condition_id))
        else:
            pool = broken or churn
            victim = min(pool, key=lambda c: (c.condition_id in previous, c.score, c.condition_id))
        group = [c for c in group if c.condition_id != victim.condition_id]
    else:
        budgets = alloc(group) if group else {}

    # Pinned names that cannot spend their share drop out of the cash split
    # so the dollar is not reserved for an order the minimum cannot fill.
    # They stay in ``group`` for the subscription.
    budgets = _release_unusable_pinned(group, alloc)

    waiting = [
        c
        for c in sorted(feasible, key=lambda c: (-c.score, c.condition_id))
        if c.condition_id not in {g.condition_id for g in group} and not c.pinned
    ]
    for cand in waiting:
        if len(group) >= max_markets:
            break
        # Prefer a new event while one is still waiting.
        same_event = bool(cand.event_id) and any(g.event_id == cand.event_id for g in group)
        other_event_waiting = any(
            w.condition_id != cand.condition_id
            and w.event_id != cand.event_id
            and not any(g.event_id == w.event_id for g in group)
            for w in waiting
        )
        if same_event and other_event_waiting:
            continue
        trial = [*group, cand]
        trial_budgets = alloc(trial)
        if not trial_budgets[cand.condition_id].fits_minimum:
            continue
        if not slice_can_rearm(trial_budgets[cand.condition_id].buy_budget_usdc, cand.min_entry_notional):
            continue
        if any(not trial_budgets[g.condition_id].fits_minimum and not g.pinned for g in group):
            continue
        if any(_would_churn(g, trial_budgets, trial) for g in trial):
            continue
        group = trial
        budgets = _release_unusable_pinned(group, alloc)
    if not budgets and group:
        budgets = alloc(group)
    return group, budgets


def _release_unusable_pinned(
    group: list[SelectCandidate],
    alloc: Callable[[list[SelectCandidate]], dict[str, BudgetSlice]],
) -> dict[str, BudgetSlice]:
    """Recompute shares after pinning a zero budget on names that cannot fit."""
    budgets = alloc(group)
    unusable = {
        c.condition_id
        for c in group
        if c.pinned and c.price_known and not budgets[c.condition_id].fits_minimum
    }
    if not unusable:
        return budgets
    fundable = [c for c in group if c.condition_id not in unusable]
    refit = alloc(fundable) if fundable else {}
    merged: dict[str, BudgetSlice] = {}
    for cand in group:
        if cand.condition_id in unusable:
            merged[cand.condition_id] = BudgetSlice(cand.condition_id, 0.0, False)
        elif cand.condition_id in refit:
            merged[cand.condition_id] = refit[cand.condition_id]
    return merged
