"""A cached, multi-instance ``KillStore`` (design §8.2, §8.9; review note).

Each gateway instance keeps the kill orders **hot** in memory so the chokepoint
check is O(1) with no network hop. Writes go to a shared durable backing store
and publish an invalidation on a ``KillBus``; subscribers reload their snapshot
within milliseconds. Because a pub/sub message can be dropped, every instance also
re-reads the monotonic ``kill_epoch`` on a periodic authoritative ``reload`` — so
a missed message self-heals. **Pub/sub for speed, polling for safety.**

``KillBus`` here is an in-process fan-out (used by the unit tests and a
single-process deployment); the Redis pub/sub bus lives in ``kill_redis``.
"""

from __future__ import annotations

from collections.abc import Callable

from acp_core.kill import KillOrder, KillScope, KillStore, KillTarget, order_matches


class KillBus:
    """A minimal publish/subscribe fan-out carrying the new ``epoch`` on each
    invalidation. In-process; swappable for Redis pub/sub (same shape)."""

    def __init__(self) -> None:
        self._subscribers: list[Callable[[int], None]] = []

    def subscribe(self, callback: Callable[[int], None]) -> None:
        self._subscribers.append(callback)

    def publish(self, epoch: int) -> None:
        for callback in list(self._subscribers):
            callback(epoch)


class CachedKillStore:
    """Wraps a durable backing ``KillStore`` with a hot in-memory snapshot.

    ``matches`` consults only the local snapshot (no backing read). The snapshot
    refreshes on a bus invalidation and on the periodic ``reload`` poll.
    """

    def __init__(self, backing: KillStore, *, bus: KillBus | None = None) -> None:
        self._backing = backing
        self._bus = bus
        self._snapshot: tuple[KillOrder, ...] = ()
        self._epoch = -1
        if bus is not None:
            bus.subscribe(self._on_invalidation)
        self.reload()

    # --- hot path --------------------------------------------------------
    def matches(self, target: KillTarget) -> KillOrder | None:
        for order in self._snapshot:
            if order_matches(order, target):
                return order
        return None

    def active_orders(self) -> tuple[KillOrder, ...]:
        return self._snapshot

    def epoch(self) -> int:
        return self._epoch

    # --- mutations (write-through + publish) -----------------------------
    def issue(
        self, scope: KillScope, *, issued_by: str, predicate: str | None = None
    ) -> KillOrder:
        order = self._backing.issue(scope, issued_by=issued_by, predicate=predicate)
        self.reload()
        self._publish()
        return order

    def lift(self, order_id: str) -> KillOrder:
        order = self._backing.lift(order_id)
        self.reload()
        self._publish()
        return order

    # --- propagation -----------------------------------------------------
    def reload(self) -> None:
        """Authoritative re-read of the durable store (pub/sub handler + the
        periodic safety poll). Refreshes the hot snapshot and the local epoch."""
        self._snapshot = self._backing.active_orders()
        self._epoch = self._backing.epoch()

    def _on_invalidation(self, epoch: int) -> None:
        # A newer epoch than ours means real work changed underneath us.
        if epoch != self._epoch:
            self.reload()

    def _publish(self) -> None:
        if self._bus is not None:
            self._bus.publish(self._backing.epoch())
