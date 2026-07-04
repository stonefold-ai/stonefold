"""M5 — the kill-race against **real Postgres** (design §8.4; invariant 5). The DoD's
critical test: a kill issued concurrently with the dispatch worker either cancels
the staged row or the send has already committed — never both-passed-and-unsent.

Driven with a ``threading.Barrier`` to align the kill-writer and the claim across
many iterations, over the genuine ``SELECT … FOR UPDATE`` transaction (the thing
SQLite can't demonstrate, which is why the invariants mandate Postgres here).
Also checks that a durable kill survives and propagates to a second store handle.
Skipped when psycopg / testcontainers / Docker are unavailable.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from typing import Any

import pytest

os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")

pytest.importorskip("psycopg")
pytest.importorskip("testcontainers.postgres")

import psycopg  # noqa: E402
from testcontainers.postgres import PostgresContainer  # noqa: E402

from stonefold_core import Actor, Connectors, PendingState, RawCall, Session  # noqa: E402
from stonefold_core.kill import KillScope, KillTarget  # noqa: E402
from stonefold_connectors import InMemoryConnector  # noqa: E402
from stonefold_store.outbox_pg import PostgresOutboxStore, create_schema  # noqa: E402
from stonefold_store.kill_pg import PostgresKillStore, create_kill_schema  # noqa: E402
from tests.conftest import full_registry  # noqa: E402

pytestmark = pytest.mark.integration


def _connect(container: PostgresContainer) -> Any:
    return psycopg.connect(
        host=container.get_container_host_ip(),
        port=int(container.get_exposed_port(5432)),
        user="stonefold", password="stonefold", dbname="stonefold", autocommit=True,
    )


@pytest.fixture(scope="module")
def container() -> Iterator[PostgresContainer]:
    with PostgresContainer(
        "postgres:16-alpine", username="stonefold", password="stonefold", dbname="stonefold"
    ) as pg:
        conn = _connect(pg)
        create_schema(conn)
        create_kill_schema(conn)
        conn.close()
        yield pg


def _truncate(conn: Any) -> None:
    with conn.cursor() as cur:
        cur.execute("TRUNCATE pending_actions, audit_log, kill_orders")


def _resolve() -> Any:
    return full_registry().resolve(
        RawCall(resource="Email", action="sendEmail", data={"to": "x@acme.example"})
    )


def _kill_check_for(kill_conn: Any) -> Any:
    # The dispatch-time kill check reads the AUTHORITATIVE durable store inside
    # the worker's transaction (design §8.4 / §8.3 point 3).
    store = PostgresKillStore(kill_conn)
    return lambda row: store.matches(KillTarget.from_pending(row)) is not None


def _stage_one(conn: Any) -> Any:
    _truncate(conn)
    return PostgresOutboxStore(conn).stage(
        resolved=_resolve(), actor=Actor(id="alice"), session_id="s1",
        agent="support", state=PendingState.PENDING,
    )


def test_e2_kill_committed_before_claim_cancels_and_never_dispatches(
    container: PostgresContainer,
) -> None:
    # The "kill seen first" branch, deterministically: the kill is committed to
    # kill_orders BEFORE the worker opens its claim transaction.
    conn = _connect(container)
    staged = _stage_one(conn)

    conn_kill = _connect(container)
    PostgresKillStore(conn_kill).issue(KillScope.for_session("s1"), issued_by="operator")

    conn_worker = _connect(container)
    conn_check = _connect(container)
    claimed = PostgresOutboxStore(conn_worker).claim_next_pending(_kill_check_for(conn_check))

    assert claimed is None  # the in-txn kill re-check cancelled the row
    row = PostgresOutboxStore(conn).get(staged.id)
    assert row is not None and row.state is PendingState.CANCELLED
    for c in (conn, conn_kill, conn_worker, conn_check):
        c.close()


def test_e2_no_kill_claims_and_dispatches(container: PostgresContainer) -> None:
    # The "claim won" branch, deterministically: no kill ⇒ the row goes
    # DISPATCHING and the send is committed.
    conn = _connect(container)
    staged = _stage_one(conn)
    conn_worker = _connect(container)
    conn_check = _connect(container)

    claimed = PostgresOutboxStore(conn_worker).claim_next_pending(_kill_check_for(conn_check))
    assert claimed is not None and claimed.state is PendingState.DISPATCHING

    row = PostgresOutboxStore(conn).get(staged.id)
    assert row is not None and row.state is PendingState.DISPATCHING
    for c in (conn, conn_worker, conn_check):
        c.close()


def test_e2_concurrent_race_never_leaves_a_gap(container: PostgresContainer) -> None:
    # The important property: under a barrier-forced interleaving of "issue kill"
    # vs "claim", repeated many times, the row is ALWAYS in exactly one of the two
    # legal states — never the forbidden middle (kill check passed, yet the row is
    # left un-dispatched and un-cancelled). The §8.4 FOR UPDATE transaction makes
    # the kill re-check and the PENDING→DISPATCHING move atomic, so no schedule can
    # produce a gap. (Which branch wins depends on commit timing and is not
    # asserted here — the deterministic tests above cover both branches.)
    iterations = 40
    for _ in range(iterations):
        conn = _connect(container)
        staged = _stage_one(conn)

        barrier = threading.Barrier(2)
        conn_worker = _connect(container)
        conn_kill = _connect(container)
        conn_check = _connect(container)
        kill_store = PostgresKillStore(conn_kill)
        outbox_worker = PostgresOutboxStore(conn_worker)
        kill_check = _kill_check_for(conn_check)

        def issue_kill() -> None:
            barrier.wait()
            kill_store.issue(KillScope.for_session("s1"), issued_by="operator")

        claimed_holder: list[Any] = []

        def claim() -> None:
            barrier.wait()
            claimed_holder.append(outbox_worker.claim_next_pending(kill_check))

        tk = threading.Thread(target=issue_kill)
        tc = threading.Thread(target=claim)
        tk.start(); tc.start()
        tk.join(); tc.join()

        row = PostgresOutboxStore(conn).get(staged.id)
        assert row is not None
        # exactly one of the two legal outcomes — never PENDING (the gap)
        assert row.state in (PendingState.CANCELLED, PendingState.DISPATCHING)
        # and the claim result agrees with the row state
        if claimed_holder[0] is None:
            assert row.state is PendingState.CANCELLED
        else:
            assert row.state is PendingState.DISPATCHING

        for c in (conn, conn_worker, conn_kill, conn_check):
            c.close()


def test_durable_kill_visible_to_a_second_store_handle(container: PostgresContainer) -> None:
    conn_a = _connect(container)
    _truncate(conn_a)
    PostgresKillStore(conn_a).issue(KillScope.for_global(), issued_by="operator")

    # a fresh handle (a different "instance") reads the durable order
    conn_b = _connect(container)
    store_b = PostgresKillStore(conn_b)
    resolved = _resolve()
    target = KillTarget.from_resolved(resolved, Actor(id="alice"), Session(id="s9"), "support")
    assert store_b.matches(target) is not None
    assert store_b.epoch() > 0
    conn_a.close()
    conn_b.close()
