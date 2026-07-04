"""Evidence-pack export over a **real Postgres** audit_log (plan G3, read-only path).

Confirms the exporter reads records straight from the durable ``audit_log`` table the
gateway writes (``stonefold_store.audit_pg``) and builds a pack from them — without touching
the log. Skipped when psycopg / testcontainers / Docker are unavailable.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")

pytest.importorskip("psycopg")
pytest.importorskip("testcontainers.postgres")

import psycopg  # noqa: E402
from testcontainers.postgres import PostgresContainer  # noqa: E402

from stonefold_core.enums import Decision  # noqa: E402
from stonefold_core.models import AuditRecord  # noqa: E402
from stonefold_store.audit_pg import PostgresAuditSink, create_audit_schema  # noqa: E402

from stonefold_evidence import build_evidence_pack, render_markdown  # noqa: E402
from stonefold_evidence.sources import records_from_postgres  # noqa: E402

pytestmark = pytest.mark.integration

_T0 = datetime(2026, 7, 2, 9, 0, 0, tzinfo=timezone.utc)


def _connect(container: PostgresContainer) -> Any:
    return psycopg.connect(
        host=container.get_container_host_ip(),
        port=int(container.get_exposed_port(5432)),
        user="acp", password="acp", dbname="acp", autocommit=True,
    )


@pytest.fixture(scope="module")
def container() -> Iterator[PostgresContainer]:
    with PostgresContainer(
        "postgres:16-alpine", username="acp", password="acp", dbname="acp"
    ) as pg:
        conn = _connect(pg)
        create_audit_schema(conn)
        conn.close()
        yield pg


def _rec(decision: Decision, i: int, **kw: Any) -> AuditRecord:
    return AuditRecord(
        id=f"aud_{i}", timestamp=_T0 + timedelta(minutes=i), agent="ap-operator",
        actor="alice", kind="effect", resource="Payment", action="pay",
        decision=decision, correlationId="run-1", **kw,
    )


def test_export_reads_real_audit_log(container: PostgresContainer) -> None:
    conn = _connect(container)
    with conn.cursor() as cur:
        cur.execute("TRUNCATE audit_log")
    sink = PostgresAuditSink(conn)
    sink.write(_rec(Decision.ALLOW, 0, outcome="success", resultRefs=["PAY-1"]))
    sink.write(_rec(Decision.HOLD, 1, approval={"status": "pending"}))
    sink.write(_rec(Decision.HALT, 2, outcome="halted", rule="kill:k1"))

    records = records_from_postgres(conn)  # read-only SELECT
    assert len(records) == 3
    assert records[0].resultRefs == ["PAY-1"]

    pack = build_evidence_pack(records, policy_ref="p.yaml", generated_at=_T0)
    assert pack.total_records == 3
    assert pack.decision_counts == {"allow": 1, "hold": 1, "halt": 1}
    intervene = next(c for c in pack.controls if c.control.id == "art-14-intervene")
    assert intervene.present is True  # a hold and a halt are real oversight events
    assert "[VERIFY]" in render_markdown(pack)

    # the export did not mutate the log
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM audit_log")
        assert cur.fetchone()[0] == 3
    conn.close()
