"""M5 — **E3** kill propagation across instances (design §8.2, §8.9, review note).

Two gateway instances share one durable backing store. A kill issued on instance
A propagates to B fast via the pub/sub bus; and if a bus message is *dropped*, B
still self-heals on the next authoritative reload because the monotonic
``kill_epoch`` advanced. "Pub/sub for speed, polling for safety."

This exercises the mechanism in-process (a ``KillBus`` of in-memory subscribers);
``test_m5_pg_integration.py`` runs the same two-instance shape over real Postgres.
"""

from __future__ import annotations

from acp_core import Actor, RawCall, Session
from acp_core.kill import KillOrder, KillScope, KillTarget
from acp_store.kill_cached import CachedKillStore, KillBus
from acp_store.kill_memory import InMemoryKillStore
from tests.conftest import full_registry


def _target(session: str = "s1") -> KillTarget:
    resolved = full_registry().resolve(RawCall(resource="Email", action="sendEmail",
                                               data={"to": "x@acme.example"}))
    return KillTarget.from_resolved(resolved, Actor(id="alice"), Session(id=session), "support")


def test_e3_global_kill_propagates_via_pubsub() -> None:
    backing = InMemoryKillStore()
    bus = KillBus()
    a = CachedKillStore(backing, bus=bus)
    b = CachedKillStore(backing, bus=bus)

    assert b.matches(_target()) is None
    a.issue(KillScope.for_global(), issued_by="operator")  # publishes invalidation
    # B updated its hot set from the bus — no explicit reload needed
    assert b.matches(_target()) is not None


def test_e3_dropped_message_self_heals_via_epoch_reload() -> None:
    backing = InMemoryKillStore()
    bus = KillBus()
    b = CachedKillStore(backing, bus=bus)

    # Simulate a DROPPED pub/sub message: write straight to the durable store so
    # the bus never notifies B.
    backing.issue(KillScope.for_global(), issued_by="operator")
    assert b.matches(_target()) is None  # B is stale — it missed the message
    assert b.epoch() < backing.epoch()  # but the epoch advanced underneath it

    b.reload()  # the periodic authoritative reload (safety poll)
    assert b.matches(_target()) is not None  # self-healed
    assert b.epoch() == backing.epoch()


def test_e3_lift_also_propagates() -> None:
    backing = InMemoryKillStore()
    bus = KillBus()
    a = CachedKillStore(backing, bus=bus)
    b = CachedKillStore(backing, bus=bus)

    order = a.issue(KillScope.for_session("s1"), issued_by="operator")
    assert b.matches(_target("s1")) is not None
    a.lift(order.id)
    assert b.matches(_target("s1")) is None  # lift propagated too


def test_e3_hot_path_uses_local_snapshot_only() -> None:
    # matches() must not hit the backing store on the hot path (sub-microsecond).
    backing = _CountingStore()
    cached = CachedKillStore(backing, bus=None)
    before = backing.reads
    cached.matches(_target())
    cached.matches(_target())
    assert backing.reads == before  # zero backing reads on matches()


class _CountingStore(InMemoryKillStore):
    def __init__(self) -> None:
        super().__init__()
        self.reads = 0

    def active_orders(self) -> tuple[KillOrder, ...]:
        self.reads += 1
        return super().active_orders()
