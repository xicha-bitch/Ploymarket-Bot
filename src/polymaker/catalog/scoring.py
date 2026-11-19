"""Market attractiveness scoring for the scanner.

Combines the v1 reward-density intuition with the new maker-rebate income
stream and penalizes spread/extremes. Higher score = more attractive to make.
Pure functions over MarketMeta.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from polymaker.domain import MarketMeta

# Hollow Gamma books are traps for a ~$1 post-only micro maker: one 5-share
# fill can eat most of the bankroll into an air pocket. Hard-block new entry
# below the floor; heavily downrank until the soft depth mark.
ENTRY_LIQUIDITY_FLOOR = 100.0
ENTRY_LIQUIDITY_SOFT = 1000.0
# Optional live air-gap (ticks under the bid). When known and this wide, block
# new entry the same way a thin Gamma liquidity_num would.
ENTRY_AIR_GAP_TICKS = 8.0


@dataclass(frozen=True, slots=True)
class MarketScore:
    condition_id: str
    reward_density: float  # est. reward $/day per $100 of two-sided liquidity
    rebate_potential: float  # est. daily rebate $ available to makers
    spread: float
    extremity: float  # 0 = mid ~0.5 (good), 1 = near 0/1 (bad payoff asymmetry)
    score: float


def is_extremely_thin_book(
    liquidity_num: float | None,
    *,
    air_gap_ticks: float | None = None,
    liquidity_floor: float = ENTRY_LIQUIDITY_FLOOR,
    air_gap_limit: float = ENTRY_AIR_GAP_TICKS,
) -> bool:
    """True when a book is too hollow / gappy for a new micro entry.

    ``liquidity_num is None`` means unknown (do not hard-block). A known
    air-gap at or above ``air_gap_limit`` also blocks.
    """
    if liquidity_num is not None and liquidity_num < liquidity_floor:
        return True
    if air_gap_ticks is not None and air_gap_ticks >= air_gap_limit:
        return True
    return False


def book_depth_quality(
    liquidity_num: float,
    *,
    soft: float = ENTRY_LIQUIDITY_SOFT,
    floor: float = ENTRY_LIQUIDITY_FLOOR,
) -> float:
    """Depth quality in ``[0, 1]`` for selection / scanner ranking.

    Below ``floor`` → 0 (hard-block territory). Between floor and soft →
    log ramp. At/above soft → 1.
    """
    liq = max(0.0, liquidity_num)
    if liq < floor:
        return 0.0
    if soft <= floor:
        return 1.0 if liq >= soft else 0.0
    if liq >= soft:
        return 1.0
    # log ramp so slightly-above-floor books stay heavily discounted
    return min(1.0, math.log1p(liq - floor) / math.log1p(soft - floor))


def _mid(m: MarketMeta) -> float:
    if m.best_bid > 0 and m.best_ask > 0:
        return (m.best_bid + m.best_ask) / 2.0
    return 0.5


def reward_density(m: MarketMeta, quote_size_usdc: float = 100.0) -> float:
    """Rough reward $/day if we hold ~quote_size two-sided in-band.

    The exact per-order S((v-s)/v)^2 scoring depends on live competition; for
    ranking we use daily_rate scaled by how much of the (small) market our
    typical size represents, capped. This mirrors v1's gm_reward_per_100 as a
    relative ranking signal, not an absolute forecast.
    """
    if m.rewards_daily_rate <= 0 or m.rewards_max_spread <= 0:
        return 0.0
    liq = max(m.liquidity_num, quote_size_usdc)
    our_share = min(1.0, quote_size_usdc / liq)
    return m.rewards_daily_rate * our_share


def rebate_potential(m: MarketMeta) -> float:
    """Estimated daily maker-rebate POOL for the market, using the exact V2 fee
    formula (per-market rate + rebate rate, no hardcoding).

    Per-share taker fee = fee_rate * p*(1-p)  (py_clob_client_v2/fees.py).
    Daily taker shares ~ vol_24h / mid, so:
        daily fees   = (vol/mid) * fee_rate * mid*(1-mid) = vol * fee_rate * (1-mid)
        rebate pool  = daily fees * rebate_rate
    This is the whole-market pool; your take is (your maker-fill share) x pool.
    It's a trailing-volume estimate — actual depends on future flow + fill share.
    """
    if not m.fees_enabled or m.rebate_rate <= 0 or m.taker_fee_bps <= 0:
        return 0.0
    vol24 = m.volume_24hr
    if vol24 <= 0:
        return 0.0
    fee_rate = m.taker_fee_bps / 10000.0
    mid = _mid(m)
    daily_fees = vol24 * fee_rate * (1.0 - mid)
    return round(daily_fees * m.rebate_rate, 2)


def extremity(m: MarketMeta) -> float:
    """0 near 0.5 (balanced), ->1 near the 0/1 boundary (skip these)."""
    mid = _mid(m)
    return min(1.0, abs(mid - 0.5) / 0.5)


def score_market(m: MarketMeta) -> MarketScore:
    rd = reward_density(m)  # our estimated reward income (share-adjusted)
    rp = rebate_potential(m)  # total daily rebate POOL (for display)
    ext = extremity(m)
    spread = max(0.0, m.best_ask - m.best_bid) if (m.best_bid and m.best_ask) else 1.0

    # our estimated income = reward share + (rebate pool * our fill/liquidity share);
    # extremity and wide spreads discount the score
    ref = 100.0
    our_share = min(0.5, ref / max(m.liquidity_num, ref))  # you won't own a whole pool
    income = rd + rp * our_share
    penalty = (1.0 - 0.5 * ext) * (1.0 / (1.0 + spread * 20.0))
    # viability: hollow books must not game "our share" to the top of ranking
    viability = book_depth_quality(m.liquidity_num, soft=2000.0, floor=ENTRY_LIQUIDITY_FLOOR)
    return MarketScore(
        condition_id=m.condition_id,
        reward_density=round(rd, 3),
        rebate_potential=round(rp, 3),  # the market's total daily rebate pool
        spread=round(spread, 4),
        extremity=round(ext, 3),
        score=round(income * penalty * viability, 4),
    )
