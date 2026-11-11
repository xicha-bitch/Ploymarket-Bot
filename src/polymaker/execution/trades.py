"""Parse CLOB REST /data/trades rows into domain Fills (dedupe by trade_id).

Only real exchange rows become fills — never invent from local PnL/order_log.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from polymaker.domain import Fill, Side, TradeState
from polymaker.userstream.parse import normalize_trade


def _side(value: object) -> Side:
    return Side.SELL if str(value).upper() == "SELL" else Side.BUY


def _ts(row: dict[str, Any]) -> float:
    for key in ("match_time", "last_update", "timestamp", "ts"):
        raw = row.get(key)
        if raw is None:
            continue
        try:
            v = float(raw)
        except (TypeError, ValueError):
            continue
        return v / 1000.0 if v > 1e12 else v
    return 0.0


def _size_shares(raw: object) -> float | None:
    """Parse size as shares. Micro-unit integers (>=1e6 whole) are scaled /1e6."""
    try:
        v = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    # OpenAPI examples sometimes show raw 1e6 micro-shares; live WS uses shares.
    if v >= 1_000_000 and abs(v - round(v)) < 1e-9:
        return v / 1e6
    return v


def _status_ok(row: dict[str, Any]) -> bool:
    status = str(row.get("status", "")).upper()
    if not status:
        return True
    if status in ("FAILED", "TRADE_STATUS_FAILED"):
        return False
    return True


def fills_from_rest_trade(
    row: dict[str, Any],
    our_address: str,
    other_token: Callable[[str], str | None],
) -> list[Fill]:
    """Map one REST trade object to zero-or-more Fills for *our* maker/taker legs."""
    if not isinstance(row, dict) or not _status_ok(row):
        return []

    makers = row.get("maker_orders") or []
    if isinstance(makers, list) and makers:
        # Reuse WS normalizer; treat settled REST rows as MATCHED for extraction.
        msg = dict(row)
        status = str(msg.get("status", "")).upper()
        if status.startswith("TRADE_STATUS_"):
            status = status[len("TRADE_STATUS_") :]
        if status in ("CONFIRMED", "MINED", ""):
            msg["status"] = "MATCHED"
        elif status not in ("MATCHED", "MINED", "CONFIRMED", "RETRYING", "FAILED"):
            msg["status"] = "MATCHED"
        evs = normalize_trade(msg, our_address, other_token)
        return [
            Fill(
                token_id=ev.token_id,
                side=ev.our_side,
                price=ev.price,
                size=ev.size,
                trade_id=ev.trade_id,
                ts=ev.ts or _ts(row),
                is_maker=True,
            )
            for ev in evs
            if ev.status is not TradeState.FAILED and ev.size > 0
        ]

    # Flat authenticated shape: trader_side says whether we were maker/taker.
    trade_id = str(row.get("id", "") or "")
    if not trade_id:
        return []
    token = str(row.get("asset_id", "") or "")
    if not token:
        return []
    size = _size_shares(row.get("size", row.get("matched_amount")))
    try:
        price = float(row.get("price", 0))
    except (TypeError, ValueError):
        return []
    if size is None or price <= 0:
        return []

    taker_side = _side(row.get("side"))
    trader_side = str(row.get("trader_side", "MAKER")).upper()
    if trader_side == "TAKER":
        our_side = taker_side
        is_maker = False
    else:
        our_side = taker_side.opposite
        is_maker = True

    # Optional: maker_address filter when present and we were maker.
    maker_addr = str(row.get("maker_address", "") or "").lower()
    if is_maker and maker_addr and maker_addr != our_address.lower():
        return []

    return [
        Fill(
            token_id=token,
            side=our_side,
            price=price,
            size=size,
            trade_id=trade_id,
            ts=_ts(row),
            is_maker=is_maker,
        )
    ]
