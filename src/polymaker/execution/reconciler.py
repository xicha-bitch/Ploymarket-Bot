"""Pure reconciliation: desired TargetQuotes vs live orders -> minimal actions.

The strategy emits a target quote set; this computes the smallest cancel/place
set to reach it, applying churn tolerances so we don't burn queue position for
sub-tick or sub-threshold size changes (v1's should_cancel instinct, generalized).
No I/O — the gateway executes the returned plan.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from polymaker.domain import OpenOrder, Quote, TargetQuotes

_EPS = 1e-9


@dataclass(frozen=True, slots=True)
class ReconcilePlan:
    to_cancel: list[str] = field(default_factory=list)  # order ids
    to_place: list[Quote] = field(default_factory=list)

    @property
    def is_noop(self) -> bool:
        return not self.to_cancel and not self.to_place


def reconcile(
    targets: TargetQuotes,
    live: list[OpenOrder],
    *,
    tick: float,
    reprice_ticks: int,
    resize_frac: float,
    protect_bid_prices: dict[tuple[str, str], float] | None = None,
) -> ReconcilePlan:
    """Diff targets against live orders. Keep live orders that already satisfy a
    target within tolerance; cancel the rest; place targets with no match.

    ``protect_bid_prices`` maps ``(token_id, side)`` to the price we would bid
    now. A resting post-only BUY is kept when that price has not moved by more
    than ``reprice_ticks``, even if this pass emitted no matching target
    (leftover slice below one more minimum). A move past ``reprice_ticks``
    still cancels. Sells are never protected.
    """
    live_by_key: dict[tuple[str, str], list[OpenOrder]] = defaultdict(list)
    for o in live:
        live_by_key[(o.token_id, o.side.value)].append(o)

    keep: set[str] = set()
    to_place: list[Quote] = []
    price_tol = reprice_ticks * tick + _EPS

    for q in targets.quotes:
        candidates = live_by_key.get((q.token_id, q.side.value), [])
        match: OpenOrder | None = None
        for o in candidates:
            if o.order_id in keep:
                continue
            price_close = abs(o.price - q.price) <= price_tol
            size_close = q.size <= 0 or abs(o.size - q.size) <= resize_frac * q.size + _EPS
            if price_close and size_close:
                match = o
                break
        if match is not None:
            keep.add(match.order_id)
        else:
            to_place.append(q)

    to_cancel = [o.order_id for o in live if o.order_id not in keep]
    if protect_bid_prices:
        protected: list[str] = []
        still_cancel: list[str] = []
        by_id = {o.order_id: o for o in live}
        for oid in to_cancel:
            order = by_id.get(oid)
            if order is None or order.side.value != "BUY":
                still_cancel.append(oid)
                continue
            ref = protect_bid_prices.get((order.token_id, order.side.value))
            if ref is None or abs(order.price - ref) > price_tol:
                still_cancel.append(oid)
                continue
            protected.append(oid)
        if protected:
            # The live bid already represents a quote at this price. Do not
            # also place a twin on top of the order we just decided to keep.
            protected_keys = {
                (by_id[oid].token_id, by_id[oid].side.value, by_id[oid].price) for oid in protected
            }
            to_place = [
                q
                for q in to_place
                if not any(
                    q.token_id == tok and q.side.value == side and abs(q.price - px) <= price_tol
                    for tok, side, px in protected_keys
                )
            ]
            to_cancel = still_cancel
    return ReconcilePlan(to_cancel=to_cancel, to_place=to_place)
