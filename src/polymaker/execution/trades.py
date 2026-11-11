"""Parse CLOB REST /data/trades rows into domain Fills (dedupe by trade_id).

Only real exchange rows become fills — never invent from local PnL/order_log.

Critical REST shape notes (live 2026-11-10 sample):
  * Top-level ``size`` on a MAKER trade is the **taker aggregate**, not our fill.
  * Our size/price/side live on the matching ``maker_orders[]`` entry.
  * Top-level ``maker_address`` / ``owner`` may be neither signer nor funder —
    do not use them as the sole membership filter; match maker legs by our
    addresses (signer/funder) and/or our known order ids.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from polymaker.domain import Fill, Side


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


def _norm_addr(value: object) -> str:
    return str(value or "").strip().lower()


def _identity_set(*addrs: str | None) -> set[str]:
    out: set[str] = set()
    for a in addrs:
        n = _norm_addr(a)
        if n.startswith("0x") and n != "0xpaper":
            out.add(n)
    return out


def _maker_order_id(mo: dict[str, Any]) -> str:
    for key in ("order_id", "oid", "id", "orderId", "maker_order_id"):
        raw = mo.get(key)
        if raw is not None and str(raw) != "":
            return str(raw)
    return ""


def _maker_matched_size(mo: dict[str, Any]) -> float | None:
    for key in ("matched_amount", "matched", "size_matched", "amount_matched"):
        if key in mo and mo.get(key) is not None:
            return _size_shares(mo.get(key))
    # Some REST rows only expose the matched size as ``size``.
    return _size_shares(mo.get("size"))


def _is_our_maker_leg(
    mo: dict[str, Any],
    *,
    identities: set[str],
    our_order_ids: set[str],
) -> bool:
    oid = _maker_order_id(mo)
    if oid and oid in our_order_ids:
        return True
    addr = _norm_addr(mo.get("maker_address", mo.get("owner")))
    return bool(addr and addr in identities)


def fills_from_rest_trade(
    row: dict[str, Any],
    our_address: str,
    other_token: Callable[[str], str | None],
    *,
    funder_address: str | None = None,
    our_order_ids: Iterable[str] | None = None,
) -> list[Fill]:
    """Map one REST trade object to zero-or-more Fills for *our* legs only."""
    if not isinstance(row, dict) or not _status_ok(row):
        return []

    identities = _identity_set(our_address, funder_address)
    order_ids = {str(x) for x in (our_order_ids or ()) if str(x)}
    trade_id = str(row.get("id", "") or "")
    if not trade_id:
        return []
    ts = _ts(row)
    taker_asset = str(row.get("asset_id", "") or "")
    taker_side = _side(row.get("side"))
    taker_outcome = row.get("outcome")
    trader_side = str(row.get("trader_side", "")).upper()

    makers = row.get("maker_orders") or []
    if isinstance(makers, list) and makers:
        out: list[Fill] = []
        for i, mo in enumerate(makers):
            if not isinstance(mo, dict):
                continue
            if not _is_our_maker_leg(mo, identities=identities, our_order_ids=order_ids):
                continue
            size = _maker_matched_size(mo)
            try:
                price = float(mo.get("price", 0))
            except (TypeError, ValueError):
                continue
            if size is None or size <= 0 or price <= 0:
                continue

            oid = _maker_order_id(mo)
            # Prefer explicit maker-order side when present (live REST sample).
            if mo.get("side") is not None:
                our_side = _side(mo.get("side"))
                # Same-outcome sell/buy lands on the taker asset; mint/other
                # outcome needs the opposite token when outcomes differ.
                mo_outcome = mo.get("outcome")
                if mo_outcome is not None and taker_outcome is not None and mo_outcome != taker_outcome:
                    token = other_token(taker_asset) or taker_asset
                else:
                    token = taker_asset
            elif mo.get("outcome") == taker_outcome or mo.get("outcome") is None:
                token = taker_asset
                our_side = taker_side.opposite
            else:
                token = other_token(taker_asset) or taker_asset
                our_side = taker_side

            if not token:
                continue
            # Durable id includes order id so coverage can prove this leg landed.
            if oid:
                fill_id = f"{trade_id}:mo:{oid}"
            elif len(makers) > 1:
                fill_id = f"{trade_id}:{i}"
            else:
                fill_id = trade_id
            out.append(
                Fill(
                    token_id=token,
                    side=our_side,
                    price=price,
                    size=size,
                    trade_id=fill_id,
                    ts=ts,
                    is_maker=True,
                )
            )
        return out

    # No maker_orders[] — only safe when we were the taker (top-level size is ours).
    # MAKER without maker_orders must NOT use top-level size (taker aggregate).
    if trader_side == "MAKER" or trader_side == "":
        return []
    if trader_side != "TAKER":
        return []
    if not taker_asset:
        return []
    size = _size_shares(row.get("size", row.get("matched_amount")))
    try:
        price = float(row.get("price", 0))
    except (TypeError, ValueError):
        return []
    if size is None or price <= 0:
        return []
    return [
        Fill(
            token_id=taker_asset,
            side=taker_side,
            price=price,
            size=size,
            trade_id=trade_id,
            ts=ts,
            is_maker=False,
        )
    ]
