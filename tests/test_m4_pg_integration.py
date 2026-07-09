"""M4 — the outbox against **real Postgres** via testcontainers (DoD, invariants 4–6).

Exercises the durable ``pending_actions`` table: staging, the ``SELECT … FOR
UPDATE SKIP LOCKED`` claim (no row claimed twice), exactly-once dispatch, the
settle-with-audit-in-one-transaction, and the approval release. Skipped when
psycopg / testcontainers / Docker are unavailable.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest

os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")

pytest.importorskip("psycopg")
pytest.importorskip("testcontainers.postgres")

import psycopg  # noqa: E402
from testcontainers.postgres import PostgresContainer  # noqa: E402

from stonefold_core import Actor, Connectors, PendingState, RawCall  # noqa: E402
from stonefold_connectors import InMemoryConnector  # noqa: E402
from stonefold_store import DispatchWorker  # noqa: E402
from stonefold_store.outbox_pg import PostgresOutboxStore, create_schema  # noqa: E402
from tests.conftest import full_registry  # noqa: E402

pytestmark = pytest.mark.integration


def _connect(container: PostgresContainer) -> Any:
    return psycopg.connect(
        host=container.get_container_host_ip(),
        port=int(container.get_exposed_port(5432)),
        user="stonefold",
        password="stonefold",
        dbname="stonefold",
        autocommit=True,
    )


@pytest.fixture(scope="module")
def container() -> Iterator[PostgresContainer]:
    with PostgresContainer(
        "postgres:16-alpine", username="stonefold", password="stonefold", dbname="stonefold"
    ) as pg:
        conn = _connect(pg)
        create_schema(conn)
        conn.close()
        yield pg


def _truncate(conn: Any) -> None:
    with conn.cursor() as cur:
        cur.execute("TRUNCATE pending_actions, audit_log")


def _resolve(resource: str, action: str, data: dict[str, Any] | None = None) -> Any:
    return full_registry().resolve(RawCall(resource=resource, action=action, data=data or {}))


def _state(store: PostgresOutboxStore, action_id: str) -> PendingState:
    row = store.get(action_id)
    assert row is not None
    return row.state


def test_stage_dispatch_settle_with_audit(container: PostgresContainer) -> None:
    conn = _connect(container)
    _truncate(conn)
    store = PostgresOutboxStore(conn)
    effect_conn = InMemoryConnector()
    worker = DispatchWorker(store, Connectors({"email": effect_conn}), registry=full_registry())

    staged = store.stage(
        resolved=_resolve("Email", "sendEmail", {"to": "x@acme.example"}),
        actor=Actor(id="alice"),
        session_id="s1",
        agent="support",
        state=PendingState.PENDING,
    )
    assert _state(store, staged.id) is PendingState.PENDING

    assert worker.drain() == 1
    assert _state(store, staged.id) is PendingState.DONE
    assert len(effect_conn.effects) == 1

    # settle wrote the audit row in the same transaction (invariant 6)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM audit_log")
        assert cur.fetchone()[0] == 1
    # a re-drain finds nothing PENDING ⇒ no double-dispatch
    assert worker.drain() == 0
    assert len(effect_conn.effects) == 1
    conn.close()


def test_for_update_claims_each_row_once(container: PostgresContainer) -> None:
    # Two stores on two connections claim concurrently; a single PENDING row must
    # go to exactly one of them (FOR UPDATE SKIP LOCKED).
    conn = _connect(container)
    _truncate(conn)
    seed = PostgresOutboxStore(conn)
    seed.stage(
        resolved=_resolve("Email", "sendEmail", {"to": "x@acme.example"}),
        actor=Actor(id="alice"),
        session_id="s1",
        agent="support",
        state=PendingState.PENDING,
    )

    conn_a, conn_b = _connect(container), _connect(container)
    claims = [PostgresOutboxStore(conn_a).claim_next_pending(),
              PostgresOutboxStore(conn_b).claim_next_pending()]
    got = [c for c in claims if c is not None]
    assert len(got) == 1  # exactly one worker claimed the row
    assert got[0].state is PendingState.DISPATCHING
    conn_a.close()
    conn_b.close()
    conn.close()


def test_stale_decision_cancelled_inside_claim(container: PostgresContainer) -> None:
    # v0.4 CS-017 (D5 over real Postgres): an expired row is cancelled inside the
    # FOR UPDATE claim transaction — with its audit record — and the scan moves on
    # to the next fresh row.
    from datetime import datetime, timedelta, timezone

    conn = _connect(container)
    _truncate(conn)
    store = PostgresOutboxStore(conn)
    effect_conn = InMemoryConnector()
    t0 = datetime(2026, 7, 2, 9, 0, tzinfo=timezone.utc)
    worker = DispatchWorker(
        store,
        Connectors({"email": effect_conn}),
        registry=full_registry(),
        clock=lambda: t0 + timedelta(hours=1),
    )

    stale = store.stage(
        resolved=_resolve("Email", "sendEmail", {"to": "x@acme.example"}),
        actor=Actor(id="alice"),
        session_id="s1",
        agent="support",
        state=PendingState.PENDING,
        expires_at=t0 + timedelta(minutes=30),
    )
    fresh = store.stage(
        resolved=_resolve("Email", "sendEmail", {"to": "y@acme.example"}),
        actor=Actor(id="alice"),
        session_id="s1",
        agent="support",
        state=PendingState.PENDING,
        expires_at=t0 + timedelta(hours=2),
    )

    assert worker.drain() == 1  # only the fresh row is dispatched
    assert _state(store, stale.id) is PendingState.CANCELLED
    stale_row = store.get(stale.id)
    assert stale_row is not None and stale_row.reason == "stale-decision"
    assert _state(store, fresh.id) is PendingState.DONE
    assert len(effect_conn.effects) == 1

    # both the stale cancellation and the settle wrote audit rows
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM audit_log")
        assert cur.fetchone()[0] == 2
    conn.close()


def test_b4_scope_lost_inside_the_effects_transaction(container: PostgresContainer) -> None:
    # v0.4 CS-018 (B4 over real Postgres): the SQL connector ANDs the scope
    # predicate into the effect's own UPDATE. A target reassigned to another
    # tenant between decision and dispatch ⇒ zero rows affected ⇒ FAILED
    # scope-lost, and the write commits against authorized state or not at all.
    from stonefold_connectors import SqlConnector
    from stonefold_core.scope import ScopeResolver, default_scope_registry

    conn = _connect(container)
    _truncate(conn)
    with conn.cursor() as cur:
        cur.execute(
            """DROP TABLE IF EXISTS accounts;
               CREATE TABLE accounts (
                   id text PRIMARY KEY, tenant_id text NOT NULL, balance numeric NOT NULL
               );
               INSERT INTO accounts VALUES ('A-1', 'T1', 1000), ('A-2', 'T1', 500)"""
        )

    store = PostgresOutboxStore(conn)
    sql_conn = SqlConnector(conn, effect_sql={
        "Payment.pay": (
            "UPDATE accounts SET balance = balance - %(amount)s "
            "WHERE id = %(accountId)s AND {scope}"
        ),
    })
    worker = DispatchWorker(
        store,
        Connectors({"sql": sql_conn}),
        registry=full_registry(),
        scopes=ScopeResolver({"Payment": "tenantOf"}, default_scope_registry()),
    )
    actor = Actor(id="alice", claims={"tenant": "T1"})

    lost = store.stage(
        resolved=_resolve("Payment", "pay", {"accountId": "A-1", "amount": 100}),
        actor=actor, session_id="s1", agent="pay", state=PendingState.PENDING,
    )
    ok = store.stage(
        resolved=_resolve("Payment", "pay", {"accountId": "A-2", "amount": 100}),
        actor=actor, session_id="s1", agent="pay", state=PendingState.PENDING,
    )
    # the race: A-1 moves to another tenant before dispatch
    with conn.cursor() as cur:
        cur.execute("UPDATE accounts SET tenant_id = 'T2' WHERE id = 'A-1'")

    assert worker.drain() == 2
    lost_row = store.get(lost.id)
    assert lost_row is not None and lost_row.state is PendingState.FAILED
    assert lost_row.reason == "scope-lost"
    assert _state(store, ok.id) is PendingState.DONE

    with conn.cursor() as cur:
        cur.execute("SELECT id, balance FROM accounts ORDER BY id")
        balances = dict(cur.fetchall())
        assert balances["A-1"] == 1000  # untouched — the effect never landed
        assert balances["A-2"] == 400   # the in-scope effect committed
        cur.execute("SELECT count(*) FROM audit_log")
        assert cur.fetchone()[0] == 2  # both settles audited
    conn.close()


def test_approval_release_over_postgres(container: PostgresContainer) -> None:
    conn = _connect(container)
    _truncate(conn)
    store = PostgresOutboxStore(conn)
    effect_conn = InMemoryConnector()
    worker = DispatchWorker(store, Connectors({"in_memory": effect_conn}), registry=full_registry())

    from stonefold_core import ApprovalSpec

    held = store.stage(
        resolved=_resolve("Prescribing", "prescribe", {"drug": "X"}),
        actor=Actor(id="alice"),
        session_id="s1",
        agent="rx",
        state=PendingState.PENDING_APPROVAL,
        approval=ApprovalSpec(quorum=1, approvers=("role:doctor",)),
    )
    assert worker.drain() == 0  # nothing dispatched while held
    store.approve(held.id, "dr-house")
    assert _state(store, held.id) is PendingState.PENDING
    assert worker.drain() == 1
    assert _state(store, held.id) is PendingState.DONE
    conn.close()


def test_multi_contract_release_over_postgres(container: PostgresContainer) -> None:
    """v0.6 CS-027: a row held by TWO gates promotes only when both contracts
    are satisfied — the JSONB round-trip preserves the contracts, and the
    shared ``apply_release`` runs under the row's FOR UPDATE lock."""
    conn = _connect(container)
    _truncate(conn)
    store = PostgresOutboxStore(conn)
    effect_conn = InMemoryConnector()
    worker = DispatchWorker(store, Connectors({"in_memory": effect_conn}), registry=full_registry())

    from stonefold_core import ReleaseContract

    held = store.stage(
        resolved=_resolve("Prescribing", "prescribe", {"drug": "X"}),
        actor=Actor(id="alice"),
        session_id="s1",
        agent="rx",
        state=PendingState.PENDING_APPROVAL,
        releases=(
            ReleaseContract(
                gate="precondition", cause="precondition:matchesActiveOrder",
                approvers=("role:pharmacist",), reason_code="multiple-candidates",
            ),
            ReleaseContract(
                gate="dualAuthorization", cause="dualAuthorization",
                quorum=2, dual_auth=True, distinct_from_actor=True,
                approvers=("role:clinician",),
            ),
        ),
    )
    # the actor can resolve the ambiguity contract — via the TARGETED form,
    # the only call shape that credits a check-driven contract (a bare approve
    # targets the approval-shaped contracts only, TCK J3) — but never promote
    # alone (R1)
    store.approve(held.id, "alice", gate="precondition")
    assert _state(store, held.id) is PendingState.PENDING_APPROVAL
    store.approve(held.id, "dr-house", gate="dualAuthorization")
    assert _state(store, held.id) is PendingState.PENDING_APPROVAL
    store.approve(held.id, "dr-wilson", gate="dualAuthorization")
    assert _state(store, held.id) is PendingState.PENDING

    reloaded = store.get(held.id)
    assert reloaded is not None
    by_gate = {c.gate: c for c in reloaded.releases}
    assert by_gate["precondition"].satisfied_by == ("alice",)
    assert set(by_gate["dualAuthorization"].satisfied_by) == {"dr-house", "dr-wilson"}

    assert worker.drain() == 1
    assert _state(store, held.id) is PendingState.DONE
    conn.close()
