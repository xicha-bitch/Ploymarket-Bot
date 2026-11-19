"""Bankroll fitness checks for micro-capital (~$20–30, and ~$1 live) operation."""

from __future__ import annotations

from dataclasses import dataclass

from polymaker.config import Config
from polymaker.fees import rebate_payout_warning

# A ~$1 book cannot post several 5-share orders inside the $20–30 concentration
# ratios. Auto-selection (max_markets >= 2, bankroll <= $5) may use the whole
# bankroll per market, per base size, and as the daily-loss kill. The operator
# accepts losing that book. Total exposure still cannot exceed the bankroll.
_MICRO_SPREAD_BANKROLL_USDC = 5.0


@dataclass(frozen=True)
class CapitalReport:
    bankroll_usdc: float
    max_total_exposure_usdc: float
    max_market_notional_usdc: float
    daily_loss_kill_usdc: float
    ok: bool
    warnings: list[str]
    tips: list[str]


def _micro_spread(cfg: Config, bankroll_usdc: float) -> bool:
    """True when auto-rotation may deploy a tiny bankroll across markets."""
    sel = cfg.selection
    return bool(
        sel.auto and sel.max_markets >= 2 and 0.0 < bankroll_usdc <= _MICRO_SPREAD_BANKROLL_USDC
    )


def assess_capital(cfg: Config, bankroll_usdc: float) -> CapitalReport:
    warnings: list[str] = []
    tips: list[str] = []
    risk = cfg.risk
    spread = _micro_spread(cfg, bankroll_usdc)
    # 60% / 25% keep one name from dominating a $20–30 book. On a ~$1 auto
    # book those ratios reject the only sizes that can fill a 5-share minimum,
    # and a 25% daily kill stops the book while cash is still available.
    # This operator accepts losing the micro book, so the kill may equal it.
    market_ratio = 1.0 if spread else 0.6
    base_ratio = 1.0 if spread else 0.25
    kill_ratio = 1.0 if spread else 0.25

    if bankroll_usdc <= 0:
        warnings.append("Bankroll must be > 0.")
    if risk.max_total_exposure_usdc > bankroll_usdc:
        warnings.append(
            f"max_total_exposure_usdc ({risk.max_total_exposure_usdc}) exceeds bankroll "
            f"({bankroll_usdc}). Lower risk caps or fund more."
        )
    if risk.max_market_notional_usdc > bankroll_usdc * market_ratio:
        if spread:
            warnings.append(
                "max_market_notional_usdc exceeds bankroll — per-market cap must stay "
                "within available funds."
            )
        else:
            warnings.append(
                "max_market_notional_usdc is >60% of bankroll — a single market can dominate risk."
            )
    if risk.daily_loss_kill_usdc > bankroll_usdc * kill_ratio:
        if spread:
            warnings.append(
                "daily_loss_kill_usdc exceeds bankroll — the kill switch cannot be "
                "larger than the book."
            )
        else:
            warnings.append(
                "daily_loss_kill_usdc is >25% of bankroll — tighten the kill switch for micro capital."
            )

    for name, profile in cfg.profiles.items():
        if profile.base_size_usdc > bankroll_usdc * base_ratio:
            warnings.append(
                f"profile {name!r} base_size_usdc={profile.base_size_usdc} is large vs bankroll."
            )
        if profile.q_max_usdc > bankroll_usdc:
            warnings.append(
                f"profile {name!r} q_max_usdc={profile.q_max_usdc} exceeds bankroll."
            )

    rebate_warn = rebate_payout_warning(estimated_daily_rebate_usdc=0.2)
    if rebate_warn:
        tips.append(rebate_warn)

    tips.append(
        "Live-only: set LIVE_ENABLE=1 + PK/BROWSER_ADDRESS in local .env, then "
        "`polymaker doctor` → `polymaker run` (micro caps enforced)."
    )
    tips.append("Prefer post-only / maker path; taker copy burns edge on fees + latency.")
    tips.append(
        "Panic: `polymaker cancel-all` needs wallet only (LIVE_ENABLE not required). "
        "Never paste keys into chat."
    )

    ok = len(warnings) == 0 and bankroll_usdc > 0
    return CapitalReport(
        bankroll_usdc=bankroll_usdc,
        max_total_exposure_usdc=risk.max_total_exposure_usdc,
        max_market_notional_usdc=risk.max_market_notional_usdc,
        daily_loss_kill_usdc=risk.daily_loss_kill_usdc,
        ok=ok,
        warnings=warnings,
        tips=tips,
    )
