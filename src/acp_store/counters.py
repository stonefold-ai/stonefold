"""Sliding-window counters for the store-backed gates (design §6).

Four gates need state across calls: ``rate``, ``quota``, ``quantityCap`` (count
hits in a window) and ``spendLimit`` (sum amounts in a window). Both reduce to a
**sliding window over (timestamp, amount) events** keyed by a string the gate
composes (``agent:action[:per]``). This module defines the ``CounterStore``
protocol and an in-memory implementation; the Redis implementation lives in
``acp_store.redis_store``.

Determinism (invariant 1): the *current time* is always passed in (``now``),
never read from a wall clock here — the caller injects it from ``RequestEnv``.
"""

from __future__ import annotations

from typing import Protocol


class CounterStore(Protocol):
    """A sliding-window event counter. Implementations MUST prune events older
    than ``window_s`` and MUST fail closed (raise) rather than silently return
    zero when the backing store is unreachable (design §13)."""

    def hit(self, key: str, now: float, window_s: float) -> int:
        """Record one event at ``now`` and return the number of events for
        ``key`` within the last ``window_s`` seconds (including this one)."""
        ...

    def add(self, key: str, amount: float, now: float, window_s: float) -> float:
        """Record an event of magnitude ``amount`` at ``now`` and return the sum
        of amounts for ``key`` within the last ``window_s`` seconds."""
        ...


class InMemoryCounterStore:
    """A dict-backed ``CounterStore`` for tests and single-process runs.

    Not durable and not shared across processes — that is what the Redis store
    is for — but it implements the exact same sliding-window semantics so the
    gate unit tests and the Redis integration tests assert identical behaviour.
    """

    def __init__(self) -> None:
        self._events: dict[str, list[tuple[float, float]]] = {}

    def _prune_and_record(
        self, key: str, amount: float, now: float, window_s: float
    ) -> list[tuple[float, float]]:
        cutoff = now - window_s
        events = [e for e in self._events.get(key, []) if e[0] > cutoff]
        events.append((now, amount))
        self._events[key] = events
        return events

    def hit(self, key: str, now: float, window_s: float) -> int:
        return len(self._prune_and_record(key, 1.0, now, window_s))

    def add(self, key: str, amount: float, now: float, window_s: float) -> float:
        events = self._prune_and_record(key, amount, now, window_s)
        return sum(a for _, a in events)

    def reset(self) -> None:
        """Test helper: drop all counters."""
        self._events.clear()
