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
        if cost <= remaining + _EPS:
            kept_buys.append(q)
            remaining -= cost
        else:
            reason = "infeasible" if cost > headroom_usdc + _EPS else "exhausted"
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
