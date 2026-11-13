"""Pre-place collateral / exposure budget gates (pure, no I/O).

Distinguishes:
  - exchange min size (hard — never place below)
  - rewards min size (soft scoring target — never force above affordable budget)
  - available collateral vs resting BUY + new BUY notionals (+ fee buffer)

Does **not** split an order below exchange min (illegal dust). Unaffordable
quotes are dropped entirely and logged by the caller.
"""

from __future__ import annotations

from dataclasses import dataclass

from polymaker.domain import Quote, Side

_EPS = 1e-9
# Float dust on a 5-share micro quote is well under one cent. A real cent
# still rejects. 10.02 * 0.015 = 0.1503 vs headroom 0.150289 is this case.
BUDGET_TOLERANCE_USDC = 0.001


@dataclass(frozen=True, slots=True)
class BudgetDecision:
    kept: tuple[Quote, ...]
    skipped: tuple[str, ...]  # human-readable diagnostics (no secrets)
    headroom_usdc: float


def quote_buy_notional(q: Quote) -> float:
    if q.side is not Side.BUY or q.size <= 0 or q.price <= 0:
        return 0.0
    return q.price * q.size


def place_headroom_usdc(
    *,
    available_usdc: float,
    resting_buy_usdc: float,
    filled_market_usdc: float,
    filled_total_usdc: float,
    max_market_notional_usdc: float,
    max_total_exposure_usdc: float,
    fee_buffer_usdc: float = 0.01,
) -> float:
    """Max additional BUY notional that may be posted right now.

    ``available_usdc`` is CLOB collateral (already reflects filled spends;
    resting may or may not be reserved by the API — we subtract resting
    ourselves so we never double-allocate). Caps are vs filled inventory so
    cancel/replace of our own quotes does not self-churn reduce-only.
    """
    bal = max(0.0, available_usdc - max(0.0, resting_buy_usdc) - max(0.0, fee_buffer_usdc))
    mkt = max(0.0, max_market_notional_usdc - max(0.0, filled_market_usdc))
    tot = max(0.0, max_total_exposure_usdc - max(0.0, filled_total_usdc))
    return max(0.0, min(bal, mkt, tot))


@dataclass(frozen=True, slots=True)
class BudgetSliceInput:
    """One selected market asking for a share of BUY budget."""

    condition_id: str
    event_id: str | None
    min_entry_notional: float
    filled_usdc: float = 0.0


@dataclass(frozen=True, slots=True)
class BudgetSlice:
    """Additional resting-BUY allowance for one market (not a sized order)."""

    condition_id: str
    buy_budget_usdc: float
    fits_minimum: bool


def allocate_buy_budgets(
    markets: list[BudgetSliceInput] | tuple[BudgetSliceInput, ...],
    *,
    available_usdc: float,
    fee_buffer_usdc: float,
    max_market_notional_usdc: float,
    max_event_group_loss_usdc: float,
    max_total_exposure_usdc: float,
    filled_total_usdc: float,
    resting_buy_elsewhere_usdc: float = 0.0,
    lone_market_fraction: float = 0.60,
) -> tuple[BudgetSlice, ...]:
    """Split BUY budget across an already chosen set.

    Cash is ``available - fee buffer - resting buys we will not cancel``.
    Own resting on these markets is replaceable, so it is not subtracted
    again here; the quoter subtracts whatever is still live.

    Two or more markets get equal shares. A clipped cap (market / event /
    total) does not donate the leftover to a sibling — that would dump the
    book into one name. A single market is capped at ``lone_market_fraction``
    of the pool unless its exchange minimum is larger, and still cannot
    exceed market, event, total, or cash.
    """
    pool_cash = max(
        0.0,
        available_usdc - max(0.0, fee_buffer_usdc) - max(0.0, resting_buy_elsewhere_usdc),
    )
    total_room = max(0.0, max_total_exposure_usdc - max(0.0, filled_total_usdc))
    pool = min(pool_cash, total_room)

    if not markets or pool <= _EPS:
        return tuple(
            BudgetSlice(
                m.condition_id,
                0.0,
                fits_minimum=m.min_entry_notional <= _EPS,
            )
            for m in markets
        )

    groups: dict[str, list[BudgetSliceInput]] = {}
    for m in markets:
        groups.setdefault(m.event_id or m.condition_id, []).append(m)

    def _structural_room(m: BudgetSliceInput) -> float:
        market_room = max(0.0, max_market_notional_usdc - max(0.0, m.filled_usdc))
        sibs = groups[m.event_id or m.condition_id]
        event_filled = sum(max(0.0, s.filled_usdc) for s in sibs)
        event_room = max(0.0, max_event_group_loss_usdc - event_filled)
        event_share = event_room / len(sibs)
        return max(0.0, min(market_room, event_share))

    if len(markets) == 1:
        m = markets[0]
        cap = min(pool, _structural_room(m))
        soft = pool * min(1.0, max(0.0, lone_market_fraction))
        # Raise toward the exchange minimum only when that is what makes a
        # post possible; never past the structural cap.
        target = min(cap, max(soft, max(0.0, m.min_entry_notional)))
        if m.min_entry_notional > cap + _EPS:
            target = cap
        fits = m.min_entry_notional <= target + BUDGET_TOLERANCE_USDC
        return (BudgetSlice(m.condition_id, target, fits),)

    fair = pool / len(markets)
    out: list[BudgetSlice] = []
    for m in markets:
        target = min(fair, _structural_room(m))
        target = max(0.0, target)
        fits = m.min_entry_notional <= target + BUDGET_TOLERANCE_USDC
        out.append(BudgetSlice(m.condition_id, target, fits))
    return tuple(out)


def slice_can_rearm(slice_usdc: float, min_notional: float) -> bool:
    """True when one minimum order fits and still leaves a minimum behind.

    A minimum that consumes the slice is cancelled on the next pass: leftover
    headroom is below another 5-share order, so the target goes empty.
    """
    if min_notional <= BUDGET_TOLERANCE_USDC:
        return slice_usdc > BUDGET_TOLERANCE_USDC
    return slice_usdc + BUDGET_TOLERANCE_USDC >= (2.0 * min_notional)


def resting_quote_headroom(
    *,
    incremental_headroom: float | None,
    own_resting_buy_usdc: float,
    buy_budget_usdc: float | None,
) -> float | None:
    """Headroom for the desired resting quote, counting the order already live.

    Place-headroom subtracts our own bid. That bid *is* the quote we want to
    keep, so the next target must see it as part of the slice. Otherwise a
    just-posted 5-share order looks unaffordable and reconcile cancels it.
    """
    if incremental_headroom is None and buy_budget_usdc is None:
        return None
    own = max(0.0, own_resting_buy_usdc)
    full = own if incremental_headroom is None else incremental_headroom + own
    if buy_budget_usdc is None:
        return full
    return min(full, max(0.0, buy_budget_usdc))


def filter_quotes_to_budget(
    quotes: list[Quote] | tuple[Quote, ...],
    *,
    headroom_usdc: float,
) -> BudgetDecision:
    """Keep SELL quotes; keep BUY quotes that fit headroom without undersizing.

    Prefer cheaper BUY notionals first so a micro wallet can still post the
    affordable leg of a two-sided maker quote. Never shrink a BUY below its
    requested size (would violate exchange min / intended size) — drop instead.
    """
    sells = [q for q in quotes if q.side is Side.SELL]
    buys = [q for q in quotes if q.side is Side.BUY]
    remaining = max(0.0, headroom_usdc)
    kept_buys: list[Quote] = []
    skipped: list[str] = []

    for q in sorted(buys, key=quote_buy_notional):
        cost = quote_buy_notional(q)
        if cost <= _EPS:
            skipped.append(
                f"drop_zero_notional side=BUY px={q.price:.4f} sz={q.size:.4f}"
            )
            continue
        if cost <= remaining + BUDGET_TOLERANCE_USDC:
            kept_buys.append(q)
            remaining -= cost
        else:
            reason = (
                "infeasible"
                if cost > headroom_usdc + BUDGET_TOLERANCE_USDC
                else "exhausted"
            )
            skipped.append(
                f"budget_reject reason={reason} side=BUY px={q.price:.4f} sz={q.size:.4f} "
                f"notional={cost:.4f} headroom_left={remaining:.4f}"
            )

    # Preserve original relative order: sells first (exits), then kept buys
    # in the order they appeared among buys that survived.
    buy_ids = {id(q) for q in kept_buys}
    ordered_buys = [q for q in buys if id(q) in buy_ids]
    kept = tuple(sells + ordered_buys)
    return BudgetDecision(kept=kept, skipped=tuple(skipped), headroom_usdc=headroom_usdc)
