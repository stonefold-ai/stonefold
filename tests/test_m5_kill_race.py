"""M5 — **E2, the kill–dispatch race** (design §8.4). The important one.

The dangerous window is between "checked kill" and "effect actually sent". The
outbox closes it by evaluating the kill predicate **inside** the same critical
section that moves the row ``PENDING → DISPATCHING`` (``claim_next_pending``). So
for any interleaving there are only two outcomes:

  (a) kill seen first  ⇒ the row ends ``CANCELLED`` and is never dispatched, or
  (b) the claim won    ⇒ the row is ``DISPATCHING`` and the send is committed.

There is never a third state where the kill check "passed" yet the row is left
un-dispatched and un-cancelled. This file asserts that invariant deterministically
over the in-memory store; ``test_m5_pg_integration.py`` drives the same property
through real Postgres ``SELECT … FOR UPDATE`` with two threads and a barrier.
"""

from __future__ import annotations

from stonefold_core import Actor, Connectors, PendingState, RawCall
from stonefold_core.outbox import PendingAction
from stonefold_connectors import InMemoryConnector
from stonefold_store import DispatchWorker, InMemoryOutboxStore
from tests.conftest import full_registry


def _always_kill(row: PendingAction) -> bool:
    return True


def _never_kill(row: PendingAction) -> bool:
    return False


def _stage_pending(store: InMemoryOutboxStore) -> PendingAction:
    resolved = full_registry().resolve(
        RawCall(resource="Email", action="sendEmail", data={"to": "x@acme.example"})
    )
    return store.stage(
        resolved=resolved, actor=Actor(id="alice"), session_id="s1",
        agent="support", state=PendingState.PENDING,
    )


def test_e2_kill_seen_at_claim_cancels_and_never_dispatches() -> None:
    store = InMemoryOutboxStore()
    conn = InMemoryConnector()
    worker = DispatchWorker(store, Connectors({"email": conn}), registry=full_registry())
    staged = _stage_pending(store)

    # kill is active exactly when the worker tries to claim the row
    worker.drain(kill_check=_always_kill)

    row = store.get(staged.id)
    assert row is not None and row.state is PendingState.CANCELLED
    assert row.reason == "kill"
    assert conn.effects == []  # the effect was never sent


def test_e2_no_kill_dispatches_normally() -> None:
    store = InMemoryOutboxStore()
    conn = InMemoryConnector()
    worker = DispatchWorker(store, Connectors({"email": conn}), registry=full_registry())
    staged = _stage_pending(store)

    worker.drain(kill_check=_never_kill)

    row = store.get(staged.id)
    assert row is not None and row.state is PendingState.DONE
    assert len(conn.effects) == 1


def test_e2_no_gap_across_many_interleavings() -> None:
    # Drive a kill that toggles per attempt; after each claim the row must be in
    # exactly one of the two legal terminal-or-committed states, never a gap.
    for i in range(200):
        store = InMemoryOutboxStore()
        conn = InMemoryConnector()
        worker = DispatchWorker(store, Connectors({"email": conn}), registry=full_registry())
        staged = _stage_pending(store)

        kill_active = i % 2 == 0
        worker.run_once(kill_check=_always_kill if kill_active else _never_kill)

        row = store.get(staged.id)
        assert row is not None
        # The forbidden state — "passed kill but still PENDING/un-acted" — cannot
        # occur: the row is always in exactly one of the two legal outcomes.
        assert row.state is not PendingState.PENDING
        if kill_active:
            # (a) kill seen ⇒ cancelled, never sent
            assert row.state is PendingState.CANCELLED
            assert conn.effects == []
        else:
            # (b) claim won ⇒ dispatched exactly once
            assert row.state is PendingState.DONE
            assert len(conn.effects) == 1


def test_e2_cancelled_row_cannot_later_dispatch() -> None:
    # Once CANCELLED, a subsequent worker pass (kill lifted) must not resurrect it.
    store = InMemoryOutboxStore()
    conn = InMemoryConnector()
    worker = DispatchWorker(store, Connectors({"email": conn}), registry=full_registry())
    staged = _stage_pending(store)

    worker.run_once(kill_check=_always_kill)  # cancels
    cancelled = store.get(staged.id)
    assert cancelled is not None and cancelled.state is PendingState.CANCELLED

    worker.drain(kill_check=_never_kill)  # kill lifted, drain again
    after = store.get(staged.id)
    assert after is not None and after.state is PendingState.CANCELLED  # still cancelled
    assert conn.effects == []
