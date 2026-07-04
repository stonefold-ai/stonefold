"""In-memory ``KillStore`` (design §8.2) — the single-instance default and the
test double. Holds the kill orders in a dict and a monotonic ``epoch`` that
advances on every mutation (issue or lift) so a cached wrapper can self-heal.

Id and timestamp generation live here (the I/O layer), not in the pure matcher
(invariant 1).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from stonefold_core.kill import KillOrder, KillScope, KillTarget, order_matches


def _now() -> datetime:
    return datetime.now(timezone.utc)


class InMemoryKillStore:
    """A dict-backed kill store; ``matches`` scans the active orders in issue
    order and returns the first that halts the target."""

    def __init__(self) -> None:
        self._orders: dict[str, KillOrder] = {}
        self._order_ids: list[str] = []
        self._epoch = 0

    def issue(
        self, scope: KillScope, *, issued_by: str, predicate: str | None = None
    ) -> KillOrder:
        self._epoch += 1
        order = KillOrder(
            id=f"kill_{uuid.uuid4().hex[:12]}",
            scope=scope,
            predicate=predicate,
            issued_by=issued_by,
            issued_at=_now(),
            epoch=self._epoch,
        )
        self._orders[order.id] = order
        self._order_ids.append(order.id)
        return order

    def lift(self, order_id: str) -> KillOrder:
        order = self._orders[order_id]
        self._epoch += 1
        lifted = order.model_copy(update={"lifted_at": _now(), "epoch": self._epoch})
        self._orders[order_id] = lifted
        return lifted

    def active_orders(self) -> tuple[KillOrder, ...]:
        return tuple(self._orders[i] for i in self._order_ids if self._orders[i].active)

    def matches(self, target: KillTarget) -> KillOrder | None:
        for order in self.active_orders():
            if order_matches(order, target):
                return order
        return None

    def epoch(self) -> int:
        return self._epoch
