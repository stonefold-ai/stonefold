"""M6 — durable audit against **real Postgres** (RFC §11, design §11; invariant 6).

Two acceptance properties that only a real RDBMS can demonstrate:

* the ``PostgresAuditSink`` round-trips records and replays a run by
  ``correlationId`` in order;
* **F2** — the settle writes the outcome *and* its audit record in the **same
  transaction**: if the audit write fails the state change rolls back (no
  effect-state without a record), and after a crash between connector-success and
  settle the idempotency key makes recovery re-settle exactly once (no
  double-effect, no record-without-effect).

Skipped when psycopg / testcontainers / Docker are unavailable.
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

from stonefold_core import Actor, PendingState, RawCall, Session  # noqa: E402
from stonefold_core.audit import build_record  # noqa: E402
from stonefold_core.enums import Decision  # noqa: E402
from stonefold_core.models import AuditRecord, EvalResult  # noqa: E402
from stonefold_connectors import InMemoryConnector  # noqa: E402
from stonefold_store.audit_pg import PostgresAuditSink, create_audit_schema  # noqa: E402
from stonefold_store.outbox_pg import PostgresOutboxStore, create_schema  # noqa: E402
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
        create_audit_schema(conn)  # idempotent: audit_log already made by create_schema
        conn.close()
        yield pg


def _truncate(conn: Any) -> None:
    with conn.cursor() as cur:
        cur.execute("TRUNCATE pending_actions, audit_log")


def _resolve(resource: str, action: str, data: dict[str, Any] | None = None) -> Any:
    return full_registry().resolve(RawCall(resource=resource, action=action, data=data or {}))


def _settle_record(row: Any, outcome: str) -> AuditRecord:
    return build_record(
        agent=row.agent, actor=row.actor,
        session=Session(id=row.session_id, correlation_id=row.correlation_id),
        call=RawCall(resource=row.resolved.resource, action=row.resolved.action,
                     data=dict(row.resolved.data)),
        resolved=row.resolved,
        result=EvalResult(decision=Decision.ALLOW, rule="dispatch", gates=row.gates,
                          ticket=row.id),
        outcome=outcome,
    )


# --- the durable sink round-trips and replays -----------------------------
def test_audit_sink_roundtrip_and_replay(container: PostgresContainer) -> None:
    conn = _connect(container)
    _truncate(conn)
    sink = PostgresAuditSink(conn)

    reg = full_registry()
    for resource, action in (("Customer", "read"), ("Email", "sendEmail")):
        resolved = reg.resolve(RawCall(resource=resource, action=action,
                                       data={"to": "x@acme.example"}))
        sink.write(build_record(
            agent="support", actor=Actor(id="alice"),
            session=Session(id="s1", correlation_id="run-A"),
            call=RawCall(resource=resource, action=action),
            resolved=resolved,
            result=EvalResult(decision=Decision.ALLOW, rule="allow"),
            outcome="success",
        ))
    # a record from a different run shares the table
    sink.write(build_record(
        agent="support", actor=Actor(id="bob"),
        session=Session(id="s2", correlation_id="run-B"),
        call=RawCall(resource="Customer", action="read"),
        resolved=reg.resolve(RawCall(resource="Customer", action="read")),
        result=EvalResult(decision=Decision.DENY, rule="default-deny"),
    ))

    run_a = sink.by_correlation("run-A")
    assert [r.resource for r in run_a] == ["Customer", "Email"]  # ordered by seq
    assert all(r.correlationId == "run-A" for r in run_a)
    assert len(sink.by_correlation("run-B")) == 1
    assert len(sink.all_records()) == 3
    conn.close()


def test_audit_log_is_append_only_no_update_path(container: PostgresContainer) -> None:
    # The sink exposes only write/read — there is no update/delete method. The
    # durable guarantee (no UPDATE/DELETE grant) is a deployment concern; here we
    # assert the code surface is append-only.
    sink = PostgresAuditSink(_connect(container))
    assert not hasattr(sink, "update")
    assert not hasattr(sink, "delete")


# --- F2: settle is transactional with the audit write ---------------------
class _AuditBrokenOutbox(PostgresOutboxStore):
    """A store whose audit insert fails — to prove the state change shares the
    settle's transaction and rolls back with it."""

    def _write_audit(self, cur: Any, audit: AuditRecord) -> None:
        raise RuntimeError("audit insert failed")


def test_f2_audit_write_failure_rolls_back_the_settle(container: PostgresContainer) -> None:
    conn = _connect(container)
    _truncate(conn)
    store = PostgresOutboxStore(conn)
    staged = store.stage(
        resolved=_resolve("Email", "sendEmail", {"to": "x@acme.example"}),
        actor=Actor(id="alice"), session_id="s1", agent="support",
        state=PendingState.PENDING, correlation_id="run-F2a",
    )
    claimed = store.claim_next_pending()
    assert claimed is not None and claimed.state is PendingState.DISPATCHING

    broken = _AuditBrokenOutbox(conn)
    with pytest.raises(RuntimeError):
        broken.settle(staged.id, state=PendingState.DONE,
                      result={"ok": True}, audit=_settle_record(claimed, "success"))

    # the DONE write rolled back with the failed audit insert: still DISPATCHING,
    # and no audit row exists (no effect-state without a record).
    reader = PostgresOutboxStore(_connect(container))
    row = reader.get(staged.id)
    assert row is not None and row.state is PendingState.DISPATCHING
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM audit_log")
        assert cur.fetchone()[0] == 0
    conn.close()


def test_f2_crash_between_send_and_settle_recovers_exactly_once(
    container: PostgresContainer,
) -> None:
    conn = _connect(container)
    _truncate(conn)
    store = PostgresOutboxStore(conn)
    sink = PostgresAuditSink(conn)
    effect = InMemoryConnector()

    staged = store.stage(
        resolved=_resolve("Email", "sendEmail", {"to": "x@acme.example"}),
        actor=Actor(id="alice"), session_id="s1", agent="support",
        state=PendingState.PENDING, correlation_id="run-F2b",
    )
    claimed = store.claim_next_pending()
    assert claimed is not None

    # --- the effect leaves, then we CRASH before settling ---
    effect.dispatch(claimed.resolved, claimed.actor, claimed.idempotency_key)
    assert len(effect.effects) == 1
    assert sink.by_correlation("run-F2b") == []           # no record yet
    mid = store.get(staged.id)
    assert mid is not None and mid.state is PendingState.DISPATCHING

    # --- restart/recovery: the same idempotency key makes the resend a no-op,
    #     and the settle now writes the outcome + audit atomically ---
    effect.dispatch(claimed.resolved, claimed.actor, claimed.idempotency_key)
    assert len(effect.effects) == 1                        # NOT re-sent
    store.settle(staged.id, state=PendingState.DONE, result={"ok": True},
                 audit=_settle_record(claimed, "success"))

    done = store.get(staged.id)
    assert done is not None and done.state is PendingState.DONE
    records = sink.by_correlation("run-F2b")
    assert len(records) == 1                               # exactly one record
    assert records[0].outcome == "success"
    conn.close()
