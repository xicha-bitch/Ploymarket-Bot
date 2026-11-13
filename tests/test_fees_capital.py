"""Unit tests for micro-capital fee helpers and bankroll checks."""

from __future__ import annotations

import pytest

from polymaker.capital import assess_capital
from polymaker.config import Config, RiskConfig, SelectionConfig, StrategyProfile
from polymaker.fees import estimate_taker_fee, rebate_payout_warning


def test_taker_fee_at_mid_crypto() -> None:
    est = estimate_taker_fee(shares=5.0, price=0.5, category="crypto")
    # 5 * 0.07 * 0.5 * 0.5 = 0.0875
    assert est.maker_fee_usdc == 0.0
    assert est.taker_fee_usdc == pytest.approx(0.0875, rel=1e-6)
    assert est.notional_usdc == pytest.approx(2.5)


def test_geopolitics_zero_fee() -> None:
    est = estimate_taker_fee(shares=10.0, price=0.4, category="geopolitics")
    assert est.taker_fee_usdc == 0.0


def test_rebate_warning_for_small_accrual() -> None:
    assert rebate_payout_warning(0.2) is not None
    assert rebate_payout_warning(2.0) is None


def test_capital_check_passes_micro_defaults() -> None:
    cfg = Config(
        risk=RiskConfig(
            max_total_exposure_usdc=25.0,
            max_event_group_loss_usdc=20.0,
            max_market_notional_usdc=12.0,
            daily_loss_kill_usdc=4.0,
        ),
        profiles={
            "micro-capital": StrategyProfile(base_size_usdc=4.0, q_max_usdc=10.0),
        },
    )
    report = assess_capital(cfg, 25.0)
    assert report.ok
    assert report.warnings == []


def test_capital_check_fails_when_exposure_exceeds_bankroll() -> None:
    cfg = Config(
        risk=RiskConfig(
            max_total_exposure_usdc=450.0,
            max_event_group_loss_usdc=400.0,
            max_market_notional_usdc=400.0,
            daily_loss_kill_usdc=40.0,
        ),
        profiles={"big": StrategyProfile(base_size_usdc=100.0, q_max_usdc=200.0)},
    )
    report = assess_capital(cfg, 25.0)
    assert not report.ok
    assert any("max_total_exposure_usdc" in w for w in report.warnings)


def test_micro_auto_allows_market_and_base_within_bankroll() -> None:
    """~$1 rotation may use the whole bankroll per market, not past it."""
    cfg = Config(
        risk=RiskConfig(
            max_total_exposure_usdc=1.0,
            max_event_group_loss_usdc=0.95,
            max_market_notional_usdc=1.0,
            daily_loss_kill_usdc=0.2,
        ),
        profiles={"micro-tight": StrategyProfile(base_size_usdc=0.50, q_max_usdc=0.95)},
        selection=SelectionConfig(auto=True, max_markets=4),
    )
    report = assess_capital(cfg, 1.0)
    assert report.ok, report.warnings


def test_micro_auto_still_rejects_total_and_daily_over_bankroll() -> None:
    cfg = Config(
        risk=RiskConfig(
            max_total_exposure_usdc=1.5,
            max_event_group_loss_usdc=1.0,
            max_market_notional_usdc=1.0,
            daily_loss_kill_usdc=0.4,
        ),
        profiles={"micro-tight": StrategyProfile(base_size_usdc=0.25, q_max_usdc=0.95)},
        selection=SelectionConfig(auto=True, max_markets=4),
    )
    report = assess_capital(cfg, 1.0)
    assert not report.ok
    assert any("max_total_exposure_usdc" in w for w in report.warnings)
    assert any("daily_loss_kill_usdc" in w for w in report.warnings)


def test_micro_without_auto_keeps_sixty_percent_market_cap() -> None:
    cfg = Config(
        risk=RiskConfig(
            max_total_exposure_usdc=1.0,
            max_event_group_loss_usdc=0.95,
            max_market_notional_usdc=0.90,
            daily_loss_kill_usdc=0.2,
        ),
        profiles={"micro-tight": StrategyProfile(base_size_usdc=0.25, q_max_usdc=0.95)},
        selection=SelectionConfig(auto=False, max_markets=4),
    )
    report = assess_capital(cfg, 1.0)
    assert not report.ok
    assert any("60%" in w for w in report.warnings)
