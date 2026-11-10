"""Fail-closed LIVE_ENABLE gate — engineering verification (not a paper product)."""

from __future__ import annotations

import pytest

from polymaker.config import Config, Secrets
from polymaker.domain import MarketMeta, Quote, Side, TokenMeta
from polymaker.execution.gateway import ExecutionGateway
from polymaker.live_gate import (
    LiveGateError,
    assert_live_place_allowed,
    assert_live_start_allowed,
    assert_wallet_for_cancel,
    assess_live_place,
    is_live_enabled,
)
from polymaker.logging import redact_secrets


def _cfg(*, live: str = "", pk: str = "", addr: str = "") -> Config:
    # model_construct: avoid BaseSettings re-reading process .env / env vars.
    return Config(
        secrets=Secrets.model_construct(
            pk=pk, browser_address=addr, live_enable=live
        )
    )


def _meta() -> MarketMeta:
    return MarketMeta(
        condition_id="0xcond",
        question="Will X happen?",
        slug="will-x-happen",
        tokens=(TokenMeta("yes-token", "Yes"), TokenMeta("no-token", "No")),
        tick_size=0.01,
        neg_risk=False,
        min_order_size=5.0,
        rewards_min_size=10.0,
        rewards_max_spread=3.0,
        rewards_daily_rate=50.0,
        maker_fee_bps=0,
        taker_fee_bps=100,
        fees_enabled=True,
        end_date_iso="2028-11-07T00:00:00Z",
        event_id="evt-1",
    )


def test_default_live_enable_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LIVE_ENABLE", raising=False)
    cfg = _cfg()
    assert is_live_enabled(cfg.secrets) is False
    status = assess_live_place(cfg)
    assert status.allowed is False
    assert "LIVE_ENABLE" in status.reason


def test_live_enable_requires_exact_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LIVE_ENABLE", raising=False)
    assert is_live_enabled(_cfg(live="true").secrets) is False
    assert is_live_enabled(_cfg(live="yes").secrets) is False
    assert is_live_enabled(_cfg(live="0").secrets) is False
    assert is_live_enabled(_cfg(live="1").secrets) is True


def test_place_requires_wallet_even_when_live_enable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LIVE_ENABLE", "1")
    cfg = _cfg(live="1")
    status = assess_live_place(cfg)
    assert status.allowed is False
    assert "PK" in status.reason or "BROWSER_ADDRESS" in status.reason


def test_place_allowed_when_live_and_wallet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIVE_ENABLE", "1")
    cfg = _cfg(live="1", pk="0xabc", addr="0xdef")
    status = assess_live_place(cfg)
    assert status.allowed is True
    assert_live_place_allowed(cfg)  # does not raise


def test_assert_live_place_raises_without_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LIVE_ENABLE", raising=False)
    cfg = _cfg(pk="0xabc", addr="0xdef")
    with pytest.raises(LiveGateError, match="LIVE_ENABLE"):
        assert_live_place_allowed(cfg)


async def test_gateway_place_blocked_without_live_enable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LIVE_ENABLE", raising=False)
    cfg = _cfg(pk="0xabc", addr="0xdef")
    gw = ExecutionGateway(cfg)
    # Pretend connected so we prove the gate fires before any CLOB client call.
    gw._client = object()  # type: ignore[assignment]
    with pytest.raises(LiveGateError, match="LIVE_ENABLE"):
        await gw.place(
            [Quote(token_id="yes-token", side=Side.BUY, price=0.4, size=5.0)],
            _meta(),
        )


async def test_gateway_market_order_blocked_without_live_enable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LIVE_ENABLE", raising=False)
    cfg = _cfg(pk="0xabc", addr="0xdef")
    gw = ExecutionGateway(cfg)
    gw._client = object()  # type: ignore[assignment]
    resp = await gw.market_order("yes-token", Side.BUY, 1.0, _meta())
    assert resp["status"] == "failed"
    assert "LIVE_ENABLE" in str(resp["error"])


def test_cancel_does_not_require_live_enable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LIVE_ENABLE", raising=False)
    cfg = _cfg(pk="0xabc", addr="0xdef")
    assert_wallet_for_cancel(cfg)  # does not raise
    with pytest.raises(LiveGateError, match="LIVE_ENABLE"):
        assert_live_start_allowed(cfg)


def test_cancel_requires_wallet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LIVE_ENABLE", raising=False)
    with pytest.raises(LiveGateError, match="Cancel requires"):
        assert_wallet_for_cancel(_cfg())


def test_live_start_fails_capital_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIVE_ENABLE", "1")
    cfg = _cfg(live="1", pk="0xabc", addr="0xdef")
    # Default RiskConfig exposure 5000 >> bankroll 1 → fail closed.
    with pytest.raises(LiveGateError, match="capital|exposure|Bankroll"):
        assert_live_start_allowed(cfg, bankroll=1.0)


def test_redact_secrets_processor() -> None:
    out = redact_secrets(
        None,
        "info",
        {"event": "auth", "pk": "0xSECRET", "builder_secret": "shh", "token_id": "ok"},
    )
    assert out["pk"] == "***"
    assert out["builder_secret"] == "***"
    assert out["token_id"] == "ok"
