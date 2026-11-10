"""Local placement intent ledger for idempotent recovery (no network).

Tracks client-side place attempts so a timeout / partial ack can drive
quarantine instead of blindly re-posting the same quotes. This is an
engineering reliability primitive — not a paper/sim product mode.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from hashlib import sha1


class IntentState(str, Enum):
    PENDING = "PENDING"
    ACKED = "ACKED"
    UNKNOWN = "UNKNOWN"
    QUARANTINED = "QUARANTINED"


@dataclass(slots=True)
class PlacementIntent:
    client_key: str
    condition_id: str
    n_quotes: int
    state: IntentState = IntentState.PENDING
    acked_ids: list[str] = field(default_factory=list)
    created_ts: float = field(default_factory=time.time)


def client_key_for(condition_id: str, quotes: list[tuple[str, str, float, float]]) -> str:
    """Stable key from cid + sorted (token, side, price, size) tuples."""
    payload = "|".join(
        f"{t}:{s}:{p:.6f}:{sz:.4f}" for t, s, p, sz in sorted(quotes)
    )
    digest = sha1(f"{condition_id}|{payload}".encode(), usedforsecurity=False).hexdigest()[:16]
    return f"{condition_id[:8]}:{digest}"


class PlacementIntentStore:
    """In-memory intent ledger. Engine owns lifetime; unit-tested in isolation."""

    def __init__(self) -> None:
        self._by_key: dict[str, PlacementIntent] = {}

    def record(self, intent: PlacementIntent) -> PlacementIntent:
        self._by_key[intent.client_key] = intent
        return intent

    def get(self, client_key: str) -> PlacementIntent | None:
        return self._by_key.get(client_key)

    def ack(self, client_key: str, order_ids: list[str]) -> PlacementIntent | None:
        intent = self._by_key.get(client_key)
        if intent is None:
            return None
        intent.acked_ids = list(order_ids)
        if len(order_ids) >= intent.n_quotes and intent.n_quotes > 0:
            intent.state = IntentState.ACKED
        elif order_ids:
            intent.state = IntentState.UNKNOWN  # partial ids → treat as unknown
        else:
            intent.state = IntentState.UNKNOWN
        return intent

    def mark_unknown(self, client_key: str) -> PlacementIntent | None:
        intent = self._by_key.get(client_key)
        if intent is None:
            return None
        intent.state = IntentState.UNKNOWN
        return intent

    def mark_quarantined(self, client_key: str) -> PlacementIntent | None:
        intent = self._by_key.get(client_key)
        if intent is None:
            return None
        intent.state = IntentState.QUARANTINED
        return intent

    def should_quarantine(self, client_key: str) -> bool:
        intent = self._by_key.get(client_key)
        if intent is None:
            return False
        return intent.state in (IntentState.UNKNOWN, IntentState.PENDING) or (
            intent.state is IntentState.ACKED and len(intent.acked_ids) < intent.n_quotes
        )

    def clear(self, client_key: str) -> None:
        self._by_key.pop(client_key, None)

    def clear_condition(self, condition_id: str) -> None:
        drop = [k for k, v in self._by_key.items() if v.condition_id == condition_id]
        for k in drop:
            del self._by_key[k]
