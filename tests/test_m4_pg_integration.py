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

from acp_core import Actor, Connectors, PendingState, RawCall  # noqa: E402
from acp_connectors import InMemoryConnector  # noqa: E402
from acp_store import DispatchWorker  # noqa: E402
from acp_store.outbox_pg import PostgresOutboxStore, create_schema  # noqa: E402
from tests.conftest import full_registry  # noqa: E402

pytestmark = pytest.mark.integration


def _connect(container: PostgresContainer) -> Any:
    return psycopg.connect(
        host=container.get_container_host_ip(),
        port=int(container.get_exposed_port(5432)),
        user="acp",
        password="acp",
        dbname="acp",
        autocommit=True,
    )


@pytest.fixture(scope="module")
def container() -> Iterator[PostgresContainer]:
    with PostgresContainer(
        "postgres:16-alpine", username="acp", password="acp", dbname="acp"
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


def test_approval_release_over_postgres(container: PostgresContainer) -> None:
    conn = _connect(container)
    _truncate(conn)
    store = PostgresOutboxStore(conn)
    effect_conn = InMemoryConnector()
    worker = DispatchWorker(store, Connectors({"in_memory": effect_conn}), registry=full_registry())

    from acp_core import ApprovalSpec

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
