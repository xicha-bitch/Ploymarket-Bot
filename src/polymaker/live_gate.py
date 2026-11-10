"""Explicit LIVE authorization gate (fail-closed).

Product mode is LIVE-ONLY. New places / market orders / live-start CLI commands
require LIVE_ENABLE=1 **and** wallet secrets. Branch names are never authorization.

Emergency cancel remains reachable with wallet secrets alone so open orders can
still be pulled when LIVE_ENABLE is off (places stay blocked).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from polymaker.config import Config, Secrets


class LiveGateError(RuntimeError):
    """Raised when a live place/start path is attempted without authorization."""


def env_live_enable() -> str:
    """Raw LIVE_ENABLE from the process environment (empty if unset)."""
    return os.environ.get("LIVE_ENABLE", "").strip()


def is_live_enabled(secrets: Secrets | None = None) -> bool:
    """True only when LIVE_ENABLE is exactly ``1`` (env or Secrets)."""
    raw = env_live_enable()
    if not raw and secrets is not None:
        raw = (secrets.live_enable or "").strip()
    return raw == "1"


@dataclass(frozen=True)
class LivePlaceStatus:
    allowed: bool
    live_enable: bool
    has_wallet: bool
    reason: str


def assess_live_place(cfg: Config) -> LivePlaceStatus:
    """Fail-closed assessment for CLOB place / market-order paths."""
    live = is_live_enabled(cfg.secrets)
    wallet = cfg.secrets.has_wallet
    if not live:
        return LivePlaceStatus(
            allowed=False,
            live_enable=False,
            has_wallet=wallet,
            reason=(
                "LIVE_ENABLE is not set to 1 — refusing live place. "
                "Set LIVE_ENABLE=1 in local .env only after explicit execute authorization."
            ),
        )
    if not wallet:
        return LivePlaceStatus(
            allowed=False,
            live_enable=True,
            has_wallet=False,
            reason="Set PK and BROWSER_ADDRESS in local .env (never paste keys into chat).",
        )
    return LivePlaceStatus(allowed=True, live_enable=True, has_wallet=True, reason="ok")


def assert_live_place_allowed(cfg: Config) -> None:
    """Raise LiveGateError unless place/market-order is authorized."""
    status = assess_live_place(cfg)
    if not status.allowed:
        raise LiveGateError(status.reason)


def assert_live_start_allowed(cfg: Config, *, bankroll: float | None = None) -> None:
    """Gate for ``run`` / ``livetest`` / ``moneydoctor`` CLI entrypoints.

    Requires LIVE_ENABLE=1 and wallet. When ``bankroll`` is provided, also
    requires ``capital-check`` to pass (used by ``run``).
    """
    assert_live_place_allowed(cfg)
    if bankroll is None:
        return
    from polymaker.capital import assess_capital

    report = assess_capital(cfg, bankroll)
    if not report.ok:
        detail = "; ".join(report.warnings) or "capital-check failed"
        raise LiveGateError(f"Refusing live start: {detail}")


def assert_wallet_for_cancel(cfg: Config) -> None:
    """Emergency cancel needs a wallet, not LIVE_ENABLE."""
    if not cfg.secrets.has_wallet:
        raise LiveGateError(
            "Cancel requires PK and BROWSER_ADDRESS in local .env "
            "(LIVE_ENABLE not required for panic cancel)."
        )
