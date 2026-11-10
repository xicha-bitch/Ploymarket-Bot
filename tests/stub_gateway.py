"""In-memory execution stub for unit tests.

Engineering verification only — not a product paper/sim/dry-run mode.
The product remains LIVE-ONLY (see LIVE_ENABLE gate).
"""

from __future__ import annotations

import itertools

from polymaker.domain import OpenOrder, OrderState, Quote
from polymaker.execution.ratelimit import TokenBucket


class StubGateway:
    """Records place/cancel calls without touching the CLOB."""

    def __init__(self) -> None:
        self._ids = itertools.count(1)
        self._order_bucket = TokenBucket(rate_per_s=1000.0, burst=1000.0)
        self._hb_failures = 0
        self.placed: list[OpenOrder] = []

    @property
    def order_pressure(self) -> float:
        return self._order_bucket.pressure

    @property
    def heartbeat_failures(self) -> int:
        return self._hb_failures

    async def place(self, quotes: list[Quote], meta: object) -> list[OpenOrder]:
        out: list[OpenOrder] = []
        for q in quotes:
            o = OpenOrder(
                f"stub-{next(self._ids)}",
                q.token_id,
                q.side,
                q.price,
                q.size,
                OrderState.LIVE,
            )
            out.append(o)
            self.placed.append(o)
        return out

    async def cancel(self, order_ids: list[str]) -> bool:
        return True

    async def cancel_asset(self, asset_id: str) -> bool:
        return True

    async def cancel_all(self) -> None:
        return None

    async def heartbeat(self) -> bool:
        return True

    async def open_orders(self) -> list[OpenOrder]:
        return []

    async def collateral_balance(self) -> float:
        """Plenty of stub collateral so budget gates do not skip unit-test places."""
        return 10_000.0

    async def market_order(self, *args: object, **kwargs: object) -> dict[str, object]:
        return {"status": "stub"}

    async def token_balances(self, token_ids: list[str]) -> dict[str, float]:
        return {t: 0.0 for t in token_ids}

    async def positions(self) -> dict[str, tuple[float, float]]:
        return {}
