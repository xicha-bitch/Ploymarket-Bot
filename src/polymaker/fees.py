"""Polymarket taker fee helpers (docs.polymarket.com/trading/fees).

Maker fee rate is 0. Taker fee (USDC) ≈ shares × fee_rate × price × (1 − price),
rounded to 5 decimal places. Category rates below mirror the public docs table
as of research date 2026-11; always re-check official docs before live sizing.
"""

from __future__ import annotations

from dataclasses import dataclass

# category -> taker fee rate coefficient (maker is always 0)
TAKER_FEE_RATES: dict[str, float] = {
    "crypto": 0.07,
    "sports": 0.05,
    "finance": 0.04,
    "politics": 0.04,
    "economics": 0.05,
    "culture": 0.05,
    "weather": 0.05,
    "other": 0.05,
    "general": 0.05,
    "mentions": 0.04,
    "tech": 0.04,
    "geopolitics": 0.0,
    "combos": 0.05,
}

MAKER_REBATE_MIN_PAYOUT_USDC = 1.0


@dataclass(frozen=True)
class FeeEstimate:
    category: str
    shares: float
    price: float
    fee_rate: float
    taker_fee_usdc: float
    maker_fee_usdc: float
    notional_usdc: float

    @property
    def taker_fee_bps_of_notional(self) -> float:
        if self.notional_usdc <= 0:
            return 0.0
        return 10_000.0 * self.taker_fee_usdc / self.notional_usdc


def normalize_category(category: str) -> str:
    key = category.strip().lower()
    if key not in TAKER_FEE_RATES:
        raise ValueError(
            f"unknown category {category!r}; choose one of: {', '.join(sorted(TAKER_FEE_RATES))}"
        )
    return key


def estimate_taker_fee(shares: float, price: float, category: str = "crypto") -> FeeEstimate:
    """Estimate taker fee for a fill; maker fee is always 0."""
    if shares < 0:
        raise ValueError("shares must be >= 0")
    if not 0.0 <= price <= 1.0:
        raise ValueError("price must be in [0, 1]")
    cat = normalize_category(category)
    rate = TAKER_FEE_RATES[cat]
    raw = shares * rate * price * (1.0 - price)
    fee = round(raw, 5)
    # docs: anything smaller than 0.00001 rounds to zero
    if abs(fee) < 0.00001:
        fee = 0.0
    notional = shares * price
    return FeeEstimate(
        category=cat,
        shares=shares,
        price=price,
        fee_rate=rate,
        taker_fee_usdc=fee,
        maker_fee_usdc=0.0,
        notional_usdc=notional,
    )


def rebate_payout_warning(estimated_daily_rebate_usdc: float) -> str | None:
    """Return a warning if accrued rebate is unlikely to hit the $1 daily payout floor."""
    if estimated_daily_rebate_usdc < MAKER_REBATE_MIN_PAYOUT_USDC:
        return (
            f"Estimated daily maker rebate ${estimated_daily_rebate_usdc:.2f} is below the "
            f"${MAKER_REBATE_MIN_PAYOUT_USDC:.0f} payout floor — small bankrolls often earn $0 rebates."
        )
    return None
