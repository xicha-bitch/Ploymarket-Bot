"""Pure placement batching helpers (no I/O)."""

from __future__ import annotations

from polymaker.domain import Quote


def chunk_quotes(quotes: list[Quote], max_n: int) -> list[list[Quote]]:
    """Split quotes into batches of at most ``max_n`` (exchange batch cap)."""
    if max_n <= 0:
        raise ValueError("max_n must be positive")
    if not quotes:
        return []
    return [quotes[i : i + max_n] for i in range(0, len(quotes), max_n)]
