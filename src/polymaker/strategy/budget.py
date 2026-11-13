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

    Each market is funded to its exchange minimum first, even when that
    minimum is larger than an equal share or a $0.25 base clip. Leftover
    cash is then spread into whatever room the market and event caps still
    allow, so several cheap names stay on the book and the pool is not left
    idle. A clipped cap does not donate past that cap.

    A single market receives the whole pool up to those caps.
    ``lone_market_fraction`` is kept for callers and does not withhold cash.
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

    groups: dict[str, list[int]] = {}
    for i, m in enumerate(markets):
        groups.setdefault(m.event_id or m.condition_id, []).append(i)

    def _market_room(m: BudgetSliceInput) -> float:
        return max(0.0, max_market_notional_usdc - max(0.0, m.filled_usdc))

    def _event_room(key: str) -> float:
        filled = sum(max(0.0, markets[i].filled_usdc) for i in groups[key])
        return max(0.0, max_event_group_loss_usdc - filled)

    market_rooms = [_market_room(m) for m in markets]
    event_rooms = {key: _event_room(key) for key in groups}

    del lone_market_fraction  # caps decide the lone size; do not withhold cash

    if len(markets) == 1:
        m = markets[0]
        key = m.event_id or m.condition_id
        cap = min(pool, market_rooms[0], event_rooms[key])
        fits = m.min_entry_notional <= cap + BUDGET_TOLERANCE_USDC
        return (BudgetSlice(m.condition_id, cap, fits),)

    funded = _fund_minimums_then_leftover(
        markets, market_rooms, groups, event_rooms, pool
    )
    if funded is not None:
        return funded

    # The set cannot all meet their minimums. Equal shares (still inside
    # each cap) let the selector drop the name that does not fit.
    fair = pool / len(markets)
    out: list[BudgetSlice] = []
    for i, m in enumerate(markets):
        key = m.event_id or m.condition_id
        event_share = event_rooms[key] / len(groups[key])
        target = max(0.0, min(fair, market_rooms[i], event_share))
        fits = m.min_entry_notional <= target + BUDGET_TOLERANCE_USDC
        out.append(BudgetSlice(m.condition_id, target, fits))
    return tuple(out)


def _fund_minimums_then_leftover(
    markets: list[BudgetSliceInput] | tuple[BudgetSliceInput, ...],
    market_rooms: list[float],
    groups: dict[str, list[int]],
    event_rooms: dict[str, float],
    pool: float,
) -> tuple[BudgetSlice, ...] | None:
    """Give every name its minimum, then spread leftover up to the caps.

    A minimum may be larger than an equal share. Sibling names in one event
    still cannot sum past the event cap. Returns None when the set cannot
    all be funded, so the caller can drop the name that does not fit.
    """
    n = len(markets)
    needs = [max(0.0, m.min_entry_notional) for m in markets]
    if any(needs[i] > market_rooms[i] + BUDGET_TOLERANCE_USDC for i in range(n)):
        return None
    for key, idxs in groups.items():
        if sum(needs[i] for i in idxs) > event_rooms[key] + BUDGET_TOLERANCE_USDC:
            return None
    if sum(needs) > pool + BUDGET_TOLERANCE_USDC:
        return None
    alloc = list(needs)
    leftover = pool - sum(needs)
    if leftover > _EPS:
        market_spare = [max(0.0, market_rooms[i] - alloc[i]) for i in range(n)]
        effective = [0.0] * n
        for key, idxs in groups.items():
            spare_sum = sum(market_spare[i] for i in idxs)
            event_spare = event_rooms[key] - sum(alloc[i] for i in idxs)
            if spare_sum <= _EPS or event_spare <= _EPS:
                continue
            scale = min(1.0, event_spare / spare_sum)
            for i in idxs:
                effective[i] = market_spare[i] * scale
        spare_sum = sum(effective)
        if spare_sum > _EPS:
            if leftover <= spare_sum + _EPS:
                for i in range(n):
                    alloc[i] += leftover * (effective[i] / spare_sum)
            else:
                for i in range(n):
                    alloc[i] += effective[i]
            for i in range(n):
                alloc[i] = min(alloc[i], market_rooms[i])
    out: list[BudgetSlice] = []
    for i, m in enumerate(markets):
        target = max(0.0, alloc[i])
        fits = needs[i] <= target + BUDGET_TOLERANCE_USDC
        out.append(BudgetSlice(m.condition_id, target, fits))
    return tuple(out)


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
