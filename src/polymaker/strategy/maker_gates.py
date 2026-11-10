"""Pure maker reliability gates (no I/O).

Product boundary: this is **single-venue Polymarket market making**.
YES+NO merge edge ``1 - p - q`` is a maker-pair exit after both legs fill —
it is **not** cross-venue arbitrage. Incomplete multi-venue arb is not claimed.
"""

from __future__ import annotations

from polymaker.config import StrategyProfile
from polymaker.domain import MarketMeta
from polymaker.marketdata.orderbook import BookView

_EPS = 1e-9


def compute_exit_urgency(
    *,
    held_s: float,
    adverse: float,
    exit_urgency_s: float,
    adverse_full: float = 0.03,
) -> float:
    """Map hold time + adverse markout to exit urgency in [0, 1].

    ``held_s / exit_urgency_s`` walks toward the touch; adverse markout
    (price units underwater vs avg entry) adds urgency so stuck inventory
    does not sit forever at a passive ask.
    """
    if exit_urgency_s <= 0:
        time_u = 1.0 if held_s > 0 else 0.0
    else:
        time_u = min(1.0, max(0.0, held_s) / exit_urgency_s)
    adv = max(0.0, adverse)
    if adverse_full <= 0:
        adv_u = 1.0 if adv > 0 else 0.0
    else:
        adv_u = min(1.0, adv / adverse_full)
    return min(1.0, max(time_u, 0.5 * time_u + 0.5 * adv_u))


def bid_air_gap_ticks(view: BookView, tick: float) -> float:
    """Ticks between best bid and second bid (air pocket below the touch)."""
    if tick <= 0 or view.best_bid is None or view.second_bid is None:
        return 0.0
    return max(0.0, (view.best_bid - view.second_bid) / tick)


def size_scale_for_book(
    view: BookView,
    *,
    tick: float,
    rewards_min_size: float,
    profile: StrategyProfile,
) -> float:
    """Scale quote size for thin / gappy books.

    Returns 1.0 on deep continuous books; drops toward a min-size-only regime
    when near-touch depth is thin or there is an air pocket under the bid
    (primary live adverse-selection mode from operator notes).
    """
    thin_mult = max(0.0, profile.thin_depth_mult)
    gap_lim = max(1, profile.air_gap_ticks)
    depth = min(view.bid_depth, view.ask_depth)
    floor = max(rewards_min_size, 1.0)
    thin = depth < floor * thin_mult if thin_mult > 0 else False
    gap = bid_air_gap_ticks(view, tick) >= gap_lim
    if thin or gap:
        return 0.0  # caller clamps to reward / exchange min only
    # soft taper: depth between floor*thin_mult and 2× that
    if thin_mult <= 0:
        return 1.0
    soft = floor * thin_mult
    hard = soft * 2.0
    if depth >= hard:
        return 1.0
    if depth <= soft:
        return 0.0
    return (depth - soft) / max(hard - soft, _EPS)


def pair_edge_ok(yes_bid: float, no_bid: float, *, tick: float, min_edge_ticks: int) -> bool:
    """True iff BUY-YES @ p and BUY-NO @ q leave merge edge >= min_edge_ticks * tick."""
    need = max(0, min_edge_ticks) * tick
    return (1.0 - yes_bid - no_bid) + _EPS >= need


def should_min_size_only(
    *,
    connected: bool,
    book_age_s: float,
    depth: float,
    rewards_min_size: float,
    profile: StrategyProfile,
    quiet_age_s: float = 120.0,
) -> bool:
    """Connected + thin + long silence → min-size only (not a hard HALT).

    Keeps quiet deep books quoting full size; clamps thin silent books so a
    long-lived resting bid is not oversized into an air pocket.
    """
    if not connected:
        return False  # connection-down path is a hard halt elsewhere
    floor = max(rewards_min_size, 1.0)
    thin = depth < floor * max(profile.thin_depth_mult, 0.0)
    return thin and book_age_s >= quiet_age_s


def apply_min_size_clamp(
    size_shares: float,
    *,
    scale: float,
    meta: MarketMeta,
    profile: StrategyProfile,
) -> float:
    """Apply book scale: 0 → reward/exchange min only; (0,1) tapers; 1 unchanged."""
    if size_shares <= 0:
        return 0.0
    if scale >= 1.0 - _EPS:
        return size_shares
    reward_floor = meta.rewards_min_size * profile.reward_size_mult
    floor = max(meta.min_order_size, reward_floor)
    if scale <= _EPS:
        return round(floor, 2) if floor > 0 else 0.0
    tapered = size_shares * scale
    return round(max(tapered, floor), 2)
