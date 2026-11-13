"""Pure quote construction: (market state, inventory, params) -> TargetQuotes.

This is the deterministic core of the strategy. No I/O, no wall-clock reads
except values passed in. Everything here is exercised directly by unit tests.

Product boundary — **pure single-venue market making**, not arbitrage:
  - BUY-YES + BUY-NO merge edge ``1 - p - q`` is a maker-pair exit after both
    legs fill on Polymarket. It is **not** cross-venue arb and must not be
    documented or logged as such.
  - Incomplete multi-venue / copy / sniper paths are out of scope.

Model (see the README):
  reservation  r  = FV - skew(inventory)
  half-spread  δ  = base + c_vol·σ + c_tox·toxicity   (clamped to reward band in QUIET)
  YES entry bid   = r - δ                       (BUY YES, USDC-collateralized)
  NO  entry bid   = (1 - r) - δ                  (BUY NO; implied YES ask at r + δ)
  exits           = SELL limits on held inventory, walked toward the touch by urgency

The BUY-YES + BUY-NO pair is the canonical two-sided quote: both are bids, both
score rewards, and a filled pair merges back to USDC at locked edge 1 - p - q.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from polymaker.config import StrategyProfile
from polymaker.domain import MarketMeta, Position, Quote, Regime, Side, TargetQuotes
from polymaker.marketdata.orderbook import BookView
from polymaker.strategy.budget import BUDGET_TOLERANCE_USDC
from polymaker.strategy.maker_gates import (
    apply_min_size_clamp,
    is_min_size_feasible,
    pair_edge_ok,
    size_scale_for_book,
)

_EPS = 1e-9


def round_to_tick(price: float, tick: float, decimals: int, *, up: bool) -> float:
    """Snap a price to the tick grid, rounding up or down, clamped to (0,1)."""
    n = price / tick
    n = math.ceil(n - _EPS) if up else math.floor(n + _EPS)
    p = round(n * tick, decimals)
    return min(max(p, tick), 1.0 - tick)


def compute_fair_value(microprice: float, flow_z: float, tick: float, weight: float = 0.5) -> float:
    """Nudge the microprice by bounded signed flow. Clamped to (tick, 1-tick)."""
    fv = microprice + weight * flow_z * tick
    return min(max(fv, tick), 1.0 - tick)


@dataclass(frozen=True, slots=True)
class QuoteInputs:
    meta: MarketMeta
    regime: Regime
    fv: float  # YES fair value in (0,1)
    vol_short: float
    toxicity: float
    yes_view: BookView
    no_view: BookView
    pos_yes: Position
    pos_no: Position
    profile: StrategyProfile
    now: float
    risk_size_scale: float = 1.0  # RiskManager may throttle size in [0,1]
    yes_exit_urgency: float = 0.0  # [0,1]; engine raises with hold time / adverse drift
    no_exit_urgency: float = 0.0
    headroom_usdc: float | None = None
    # When set, size BUY orders to this notional instead of profile.base_size_usdc.
    # Auto-selection sets it to the market's slice, including a lone market,
    # so a $0.25 profile clip does not leave the allocated cash unposted.
    size_notional_usdc: float | None = None


def construct_quotes(inp: QuoteInputs) -> TargetQuotes:
    m = inp.meta
    p = inp.profile
    tick = m.tick_size
    dec = m.price_decimals
    cid = m.condition_id

    if inp.regime in (Regime.EVENT, Regime.HALTED):
        return TargetQuotes(cid, inp.regime, ())

    quotes: list[Quote] = []

    # ── inventory in YES-equivalent shares; holding NO is short YES ──────
    net_shares = inp.pos_yes.size - inp.pos_no.size
    q_max_shares = p.q_max_usdc / max(inp.fv, tick)
    u = _clamp(net_shares / q_max_shares, -1.0, 1.0) if q_max_shares > 0 else 0.0
    reward_floor = m.rewards_min_size * p.reward_size_mult  # scoring size w/ margin
    size_notional = p.base_size_usdc if inp.size_notional_usdc is None else inp.size_notional_usdc
    # An explicit slice is cash we intend to post. A thin book must not
    # shrink it back to a 5-share dust bid and leave the rest idle.
    deploy_slice = inp.size_notional_usdc is not None

    skew = p.gamma * inp.vol_short * u

    # ── half-spread ─────────────────────────────────────────────────────
    base = p.delta_min_ticks * tick
    delta = base + p.c_vol * inp.vol_short + p.c_tox * inp.toxicity
    reward_band = m.rewards_max_spread / 100.0
    if inp.regime == Regime.QUIET and reward_band > 0:
        delta = _clamp(delta, base, max(base, reward_band))
    delta = max(delta, tick)

    r = inp.fv - skew
    yes_bid_target = r - delta
    no_bid_target = (1.0 - r) - delta

    # ── size scaling ────────────────────────────────────────────────────
    regime_scale = 0.5 if inp.regime == Regime.TRENDING else 1.0
    tox_scale = 1.0 / (1.0 + inp.toxicity * 10.0)
    common_scale = regime_scale * tox_scale * _clamp(inp.risk_size_scale, 0.0, 1.0)
    # thin/gappy books → exchange-min only (rewards size is soft, not hard)
    yes_book_scale = size_scale_for_book(
        inp.yes_view, tick=tick, rewards_min_size=m.rewards_min_size, profile=p
    )
    no_book_scale = size_scale_for_book(
        inp.no_view, tick=tick, rewards_min_size=m.rewards_min_size, profile=p
    )

    soft_cap = p.q_soft_frac  # fraction of q_max at which the adding side pulls
    add_yes = inp.regime not in (Regime.REDUCE_ONLY,) and u < soft_cap
    add_no = inp.regime not in (Regime.REDUCE_ONLY,) and u > -soft_cap

    yes_entry: Quote | None = None
    no_entry: Quote | None = None

    # entry: BUY YES
    if add_yes:
        price = _place_bid(yes_bid_target, inp.yes_view, tick, dec, inp.fv, p.min_edge_ticks)
        if price is not None and is_min_size_feasible(
            price, min_order_size=m.min_order_size, headroom_usdc=inp.headroom_usdc
        ):
            leg = notional_covering_minimum(
                size_notional, price, m.min_order_size, inp.headroom_usdc
            )
            raw = _size_shares(leg, price, common_scale * (1 - max(u, 0.0)), m)
            sized = apply_min_size_clamp(
                raw, scale=1.0 if deploy_slice else yes_book_scale, meta=m, profile=p
            )
            before = len(quotes)
            _add_layers(quotes, m.yes.token_id, Side.BUY, price, tick, dec,
                        sized, p.layers, p.layer_step_ticks, down=True,
                        exchange_min=m.min_order_size, reward_floor=reward_floor)
            if len(quotes) > before:
                yes_entry = quotes[before]  # top-of-book layer

    # entry: BUY NO
    if add_no:
        no_fv = 1.0 - inp.fv
        price = _place_bid(no_bid_target, inp.no_view, tick, dec, no_fv, p.min_edge_ticks)
        if price is not None and is_min_size_feasible(
            price, min_order_size=m.min_order_size, headroom_usdc=inp.headroom_usdc
        ):
            leg = notional_covering_minimum(
                size_notional, price, m.min_order_size, inp.headroom_usdc
            )
            raw = _size_shares(leg, price, common_scale * (1 - max(-u, 0.0)), m)
            sized = apply_min_size_clamp(
                raw, scale=1.0 if deploy_slice else no_book_scale, meta=m, profile=p
            )
            before = len(quotes)
            _add_layers(quotes, m.no.token_id, Side.BUY, price, tick, dec,
                        sized, p.layers, p.layer_step_ticks, down=True,
                        exchange_min=m.min_order_size, reward_floor=reward_floor)
            if len(quotes) > before:
                no_entry = quotes[before]

    # hard pair-cost gate: if both entry tops would leave too little merge edge,
    # drop the worse (higher) top and its deeper layers on that token
    if (
        yes_entry is not None
        and no_entry is not None
        and not pair_edge_ok(
            yes_entry.price, no_entry.price, tick=tick, min_edge_ticks=p.pair_edge_ticks
        )
    ):
        drop_tok = (
            m.yes.token_id if yes_entry.price >= no_entry.price else m.no.token_id
        )
        quotes[:] = [
            q for q in quotes
            if not (q.token_id == drop_tok and q.side is Side.BUY)
        ]

    # ── exits: SELL held inventory (maker, never cross) ─────────────────
    _maybe_exit(quotes, m.yes.token_id, inp.pos_yes, inp.fv, delta, inp.yes_view, tick, dec,
                inp.yes_exit_urgency, m, inp.regime)
    _maybe_exit(quotes, m.no.token_id, inp.pos_no, 1.0 - inp.fv, delta, inp.no_view, tick, dec,
                inp.no_exit_urgency, m, inp.regime)

    return TargetQuotes(cid, inp.regime, tuple(quotes))


# ── helpers ─────────────────────────────────────────────────────────────


def _clamp(x: float, lo: float, hi: float) -> float:
    return min(max(x, lo), hi)


def _place_bid(
    target: float, view: BookView, tick: float, dec: int, fv: float, min_edge_ticks: int
) -> float | None:
    """Position a BUY: join the touch or sit behind, never cross, keep min edge vs FV."""
    price = target
    # never bid above (FV - min_edge*tick): we don't pay through fair value
    price = min(price, fv - min_edge_ticks * tick)
    # join the queue rather than jump it (conservative maker default)
    if view.best_bid is not None and price >= view.best_bid:
        price = view.best_bid
    # never cross the ask
    if view.best_ask is not None and price >= view.best_ask:
        price = view.best_ask - tick
    p = round_to_tick(price, tick, dec, up=False)
    if p <= 0 or p >= 1:
        return None
    return p


def notional_covering_minimum(
    size_notional: float,
    price: float,
    min_order_size: float,
    headroom_usdc: float | None,
) -> float:
    """Raise a base-size clip when one exchange-minimum order costs more.

    A $0.25 base cannot post 5 shares at $0.10 ($0.50). Use that minimum
    notional when headroom can pay for it. Do not exceed headroom.
    """
    floor = max(0.0, min_order_size) * max(0.0, price)
    if floor <= size_notional + BUDGET_TOLERANCE_USDC:
        return size_notional
    if headroom_usdc is not None and floor > headroom_usdc + BUDGET_TOLERANCE_USDC:
        return size_notional
    return floor


def _size_shares(base_usdc: float, price: float, scale: float, m: MarketMeta) -> float:
    """USDC-notional sizing -> shares. Per-order minimums applied in _add_layers
    (reward scoring is per ORDER, so the floor must hold per layer, not per total)."""
    shares = (base_usdc / max(price, m.tick_size)) * max(scale, 0.0)
    return round(shares, 2) if shares > 0 else 0.0


def _add_layers(
    quotes: list[Quote], token_id: str, side: Side, top_price: float, tick: float, dec: int,
    total_size: float, layers: int, step_ticks: int, *, down: bool,
    exchange_min: float = 0.0, reward_floor: float = 0.0,
) -> None:
    """Split size across `layers` price levels stepping away from the touch.

    Hard rule: every resting order must meet ``exchange_min``. Soft rule: when
    a layer is already within reach of the rewards scoring size
    (``reward_floor`` = rewardsMinSize × mult), bump it up so it scores —
    but **never** treat rewards size as a hard floor that inflates exchange-min
    dust (that previously turned 5-share micro quotes into 200-share rejects).
    """
    if total_size <= 0:
        return
    layers = max(1, layers)
    hard_min = max(0.0, exchange_min)
    reward_target = max(0.0, reward_floor)  # soft scoring target; may be 0
    per = round(total_size / layers, 2)
    # consolidate so each layer can meet the *exchange* min (hard)
    while layers > 1 and hard_min > 0 and per < hard_min:
        layers -= 1
        per = round(total_size / layers, 2)
    # soft reward bump only when already near the scoring size
    if reward_target > hard_min and 0.5 * reward_target <= per < reward_target:
        per = reward_target
    if per < hard_min or per <= 0:
        return
    for i in range(layers):
        offset = i * step_ticks * tick
        price = top_price - offset if down else top_price + offset
        price = round(price, dec)
        if 0 < price < 1:
            quotes.append(Quote(token_id, side, price, per))


def _maybe_exit(
    quotes: list[Quote], token_id: str, pos: Position, token_fv: float, delta: float,
    view: BookView, tick: float, dec: int, urgency: float, m: MarketMeta, regime: Regime,
) -> None:
    if pos.size < m.min_order_size:
        return
    # target starts at fv + delta and walks toward best_bid + tick as urgency -> 1
    passive = token_fv + delta
    floor = (view.best_bid + tick) if view.best_bid is not None else passive
    if regime == Regime.REDUCE_ONLY:
        urgency = max(urgency, 0.5)
    target = passive * (1.0 - urgency) + floor * urgency
    # never cross down through the bid; never sell below best_bid
    if view.best_bid is not None:
        target = max(target, view.best_bid + tick)
    price = round_to_tick(target, tick, dec, up=True)
    # FLOOR (never round up): selling more than we hold is rejected by the
    # exchange -> the exit silently fails and we stay long. Floor guarantees
    # size <= held.
    size = math.floor(pos.size * 100) / 100
    if 0 < price < 1 and size >= m.min_order_size:
        quotes.append(Quote(token_id, Side.SELL, price, size))
