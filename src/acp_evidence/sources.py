"""Read audit records from a store — **read-only** (no writes, no enforcement path).

Two sources: a JSONL export (one ``AuditRecord`` per line, as ``model_dump(mode="json")``)
and a live Postgres ``audit_log`` table (the ``acp_store`` schema). Both reconstruct the
typed ``AuditRecord`` so the pack builder is store-agnostic.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from acp_core.models import AuditRecord


def records_from_jsonl(path: Path) -> list[AuditRecord]:
    """One ``AuditRecord`` per non-blank line."""
    out: list[AuditRecord] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(AuditRecord.model_validate(json.loads(line)))
    return out


def records_from_postgres(conn: Any, *, table: str = "audit_log") -> list[AuditRecord]:
    """Read every audit record in write order (append-only; RFC §11). ``conn`` is a
    live ``psycopg`` connection. A read-only ``SELECT`` — the exporter never mutates
    the log."""
    with conn.cursor() as cur:
        cur.execute(f"SELECT record FROM {table} ORDER BY seq")  # noqa: S608 - fixed table name
        rows = cur.fetchall()
    return [AuditRecord.model_validate(r[0] if isinstance(r[0], dict) else json.loads(r[0]))
            for r in rows]


def write_jsonl(path: Path, records: list[AuditRecord]) -> Path:
    """Dump records to JSONL (e.g. from an in-memory sink) so the exporter can read
    them. Convenience for tests/demos; not itself part of the pack."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record.model_dump(mode="json"), sort_keys=True) + "\n")
    return path
