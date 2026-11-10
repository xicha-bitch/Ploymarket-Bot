"""Positions / collateral readout: Data API v2 mapping, pagination, fail-closed."""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from polymaker.config import Config, Secrets, WalletConfig
from polymaker.execution.gateway import (
    ExecutionGateway,
    PositionsReadError,
    collateral_raw_to_usd,
    map_position_row,
    parse_v2_positions_page,
)
from polymaker.doctor import _extract_balance


FUNDER = "0xffbc72770000000000000000000000000000983b"
DATA_HOST = "https://data-api.polymarket.com"


def _gw(*, funder: str = FUNDER) -> ExecutionGateway:
    cfg = Config(
        secrets=Secrets(_env_file=None),
        wallet=WalletConfig(data_api_host=DATA_HOST),
    )
    gw = ExecutionGateway(cfg)
    gw._funder = funder  # noqa: SLF001 — test inject; funder ≠ signer EOA
    gw._address = "0x209c39CB000000000000000000000000000080E2"  # noqa: SLF001
    return gw


# ── units ───────────────────────────────────────────────────────────────
def test_collateral_raw_always_divides_by_1e6():
    # raw < 1e6 used to be mis-displayed as whole dollars (prior heuristic bug)
    assert collateral_raw_to_usd("934967") == pytest.approx(0.934967)
    assert collateral_raw_to_usd(1004087) == pytest.approx(1.004087)
    assert collateral_raw_to_usd(0) == 0.0
    assert _extract_balance({"balance": "934967"}) == pytest.approx(0.934967)
    assert _extract_balance({"balance": 500000}) == pytest.approx(0.5)


# ── row / page mapping ──────────────────────────────────────────────────
def test_map_position_row_v2_fields_and_dust():
    row = {
        "token_id": "tok-yes",
        "current_size": 12.5,
        "avg_price": 0.42,
        "current_value": 5.25,
    }
    assert map_position_row(row) == ("tok-yes", 12.5, 0.42)
    assert map_position_row({**row, "current_size": 0}) is None
    assert map_position_row({"token_id": "x", "current_size": "bad"}) is None


def test_parse_v2_positions_page_envelope_and_cursor():
    rows, nxt = parse_v2_positions_page(
        {
            "data": [{"token_id": "a", "current_size": 1, "avg_price": 0.5}],
            "pagination": {"limit": 1, "offset": 0, "has_more": True, "next_cursor": "CUR"},
        }
    )
    assert len(rows) == 1 and nxt == "CUR"
    rows2, nxt2 = parse_v2_positions_page(
        {"data": [], "pagination": {"limit": 50, "offset": 0, "has_more": False, "next_cursor": None}}
    )
    assert rows2 == [] and nxt2 is None


def test_parse_v2_positions_page_rejects_bad_shape_and_has_more_without_cursor():
    with pytest.raises(PositionsReadError):
        parse_v2_positions_page([{"asset": "legacy"}])  # bare v1 array
    with pytest.raises(PositionsReadError):
        parse_v2_positions_page(
            {"data": [], "pagination": {"has_more": True, "next_cursor": None}}
        )


# ── HTTP fail-closed ────────────────────────────────────────────────────
@respx.mock
def test_positions_http_410_raises_not_empty():
    """Retired v1 path used to return {}; must raise so callers don't treat as zero."""
    # Simulate calling the retired path shape via v2 host returning 410 (as v1 does).
    respx.get(f"{DATA_HOST}/v2/positions").mock(
        return_value=httpx.Response(
            410,
            json={
                "error": "Data API v1 has been retired",
                "migration": "https://data-api.polymarket.com/v2/",
            },
        )
    )
    gw = _gw()
    with pytest.raises(PositionsReadError) as ei:
        asyncio.run(gw.positions())
    assert ei.value.status_code == 410
    # Explicit: failure is not an empty dict
    assert not isinstance(ei.value, dict)


@respx.mock
def test_positions_non_200_raises():
    respx.get(f"{DATA_HOST}/v2/positions").mock(
        return_value=httpx.Response(503, json={"error": "unavailable"})
    )
    with pytest.raises(PositionsReadError) as ei:
        asyncio.run(_gw().positions())
    assert ei.value.status_code == 503


@respx.mock
def test_positions_empty_200_is_real_zero():
    respx.get(f"{DATA_HOST}/v2/positions").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [],
                "pagination": {
                    "limit": 500,
                    "offset": 0,
                    "has_more": False,
                    "next_cursor": None,
                },
            },
        )
    )
    out = asyncio.run(_gw().positions())
    assert out == {}


@respx.mock
def test_positions_maps_v2_and_queries_funder_not_eoa():
    route = respx.get(f"{DATA_HOST}/v2/positions").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "proxy_wallet": FUNDER,
                        "token_id": "111",
                        "current_size": 3.0,
                        "avg_price": 0.55,
                        "current_value": 1.65,
                    },
                    {
                        "token_id": "222",
                        "current_size": 0.0,  # dust — dropped
                        "avg_price": 0.1,
                    },
                ],
                "pagination": {
                    "limit": 500,
                    "offset": 0,
                    "has_more": False,
                    "next_cursor": None,
                },
            },
        )
    )
    out = asyncio.run(_gw().positions())
    assert out == {"111": (3.0, 0.55)}
    assert route.called
    req = route.calls.last.request
    assert req.url.params.get("user") == FUNDER
    assert "v2/positions" in str(req.url)
    # signer EOA must not be the query anchor
    assert req.url.params.get("user") != "0x209c39CB000000000000000000000000000080E2"


@respx.mock
def test_positions_pagination_follows_cursor_to_completion():
    def _page(request: httpx.Request) -> httpx.Response:
        cursor = request.url.params.get("cursor")
        if cursor is None:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"token_id": "p1", "current_size": 1.0, "avg_price": 0.2},
                    ],
                    "pagination": {
                        "limit": 500,
                        "offset": 0,
                        "has_more": True,
                        "next_cursor": "PAGE2",
                    },
                },
            )
        assert cursor == "PAGE2"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"token_id": "p2", "current_size": 2.0, "avg_price": 0.3},
                ],
                "pagination": {
                    "limit": 500,
                    "offset": 1,
                    "has_more": False,
                    "next_cursor": None,
                },
            },
        )

    respx.get(f"{DATA_HOST}/v2/positions").mock(side_effect=_page)
    out = asyncio.run(_gw().positions())
    assert out == {"p1": (1.0, 0.2), "p2": (2.0, 0.3)}


@respx.mock
def test_portfolio_value_none_on_non_200_not_zero():
    respx.get(f"{DATA_HOST}/v2/value").mock(return_value=httpx.Response(410, json={"error": "gone"}))
    assert asyncio.run(_gw().portfolio_value()) is None


@respx.mock
def test_portfolio_value_maps_envelope():
    respx.get(f"{DATA_HOST}/v2/value").mock(
        return_value=httpx.Response(
            200, json={"data": {"proxy_wallet": FUNDER, "value": 12.34}}
        )
    )
    assert asyncio.run(_gw().portfolio_value()) == pytest.approx(12.34)


def test_positions_missing_funder_raises():
    gw = _gw(funder="")
    gw._address = ""  # noqa: SLF001 — no EOA fallback either
    with pytest.raises(PositionsReadError, match="no funder"):
        asyncio.run(gw.positions())
    with pytest.raises(PositionsReadError, match="no funder"):
        asyncio.run(_gw(funder="0xPAPER").positions())
