"""Postgres ``AuditSink`` (RFC §11, design §11) — the durable, append-only record.

The ``audit_log`` table (shared with the outbox's settle writes, see
``outbox_pg``) is **append-only**: the gateway DB role is granted INSERT/SELECT
only, never UPDATE/DELETE, so a record can be added but never altered or removed
— "the audit is the product's evidence" (design §11). A full agent run replays
as one ordered query by ``correlationId`` (the ``seq`` column gives a stable
total order even within the same millisecond).

``psycopg`` is imported lazily so importing this module never requires the driver
(matching ``outbox_pg`` / ``kill_pg``).
"""

from __future__ import annotations

from typing import Any

from stonefold_core.models import AuditRecord

# Kept byte-identical to the ``audit_log`` definition in ``outbox_pg`` so either
# module may create the table first (``IF NOT EXISTS`` makes the second a no-op).
_AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    seq        bigserial,
    id         text PRIMARY KEY,
    record     jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS audit_log_correlation
    ON audit_log ((record->>'correlationId'), seq);
"""


def create_audit_schema(conn: Any) -> None:
    with conn.cursor() as cur:
        cur.execute(_AUDIT_SCHEMA)


class PostgresAuditSink:
    """Append-only audit sink backed by the durable ``audit_log`` table.

    Satisfies the ``AuditSink`` protocol (``write``); ``by_correlation`` provides
    the replay query (RFC §11). Each ``write`` is its own committed transaction —
    the *settle* path writes its audit in the **same** transaction as the state
    change via the outbox store (invariant 6); this sink serves the refusal/hold
    paths, where there is no accompanying state change to share a tx with.
    """

    def __init__(self, conn: Any, table: str = "audit_log") -> None:
        self._conn = conn
        self._table = table

    def write(self, record: AuditRecord) -> None:
        from psycopg.types.json import Jsonb

        with self._conn.transaction(), self._conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {self._table} (id, record) VALUES (%s, %s)",
                (record.id, Jsonb(record.model_dump(mode="json"))),
            )

    def by_correlation(self, correlation_id: str) -> list[AuditRecord]:
        """Replay one agent run as an ordered query (RFC §11)."""
        with self._conn.cursor() as cur:
            cur.execute(
                f"""SELECT record FROM {self._table}
                    WHERE record->>'correlationId' = %s
                    ORDER BY seq""",
                (correlation_id,),
            )
            rows = cur.fetchall()
        return [AuditRecord.model_validate(r[0]) for r in rows]

    def all_records(self) -> list[AuditRecord]:
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT record FROM {self._table} ORDER BY seq")
            rows = cur.fetchall()
        return [AuditRecord.model_validate(r[0]) for r in rows]
