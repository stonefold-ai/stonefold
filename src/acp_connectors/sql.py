"""SQL connector (design §5) — the canonical "scope injected below the model".

The agent's intent is translated to SQL; the connector then **appends** the
scope predicate as an extra ``WHERE`` clause (``AND owner_id = %(scope_owner_id)s``)
that the agent never named and cannot widen. ``psycopg`` is imported lazily so the
package loads without it.
"""

from __future__ import annotations

from typing import Any

from acp_core.connector import ConnectorResult
from acp_core.enums import Kind
from acp_core.models import Actor, ResolvedAction
from acp_core.scope import ScopePredicate


class SqlConnector:
    """Reads/records against Postgres with the scope filter appended below the
    gateway. ``conn`` is a live ``psycopg.Connection``."""

    def __init__(self, conn: Any, *, table_map: dict[str, str] | None = None) -> None:
        self._conn = conn
        self._table_map = dict(table_map or {})

    def _table(self, resource: str) -> str:
        return self._table_map.get(resource, resource.lower())

    def execute(
        self, action: ResolvedAction, scope: ScopePredicate | None, actor: Actor
    ) -> ConnectorResult:
        from psycopg.rows import dict_row

        if action.kind is not Kind.OBSERVE:
            # records/transitions over SQL are minimal in M3 (B1 targets reads).
            return ConnectorResult(kind="receipt", receipt={"ok": True})

        where = "1=1"
        params: dict[str, Any] = {}
        if scope is not None:
            clause, scope_params = scope.sql_where(actor)
            where = f"{where} AND {clause}"
            params.update(scope_params)
        sql = f"SELECT * FROM {self._table(action.resource)} WHERE {where}"
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            rows = [dict(r) for r in cur.fetchall()]
        return ConnectorResult(kind="rows", rows=rows, query=sql)

    def dispatch(
        self, action: ResolvedAction, actor: Actor, idempotency_key: str
    ) -> ConnectorResult:
        # A SQL "effect" (e.g. an INSERT side effect) is rare in M3/M4; the
        # minimal form records a receipt keyed by the idempotency key.
        return ConnectorResult(kind="receipt", receipt={"sent": True}, handle=idempotency_key)

    def fetch_target(
        self, action: ResolvedAction, scope: ScopePredicate | None, actor: Actor
    ) -> dict[str, Any] | None:
        from psycopg.rows import dict_row

        camel = f"{action.resource[:1].lower()}{action.resource[1:]}Id"
        target_id = action.data.get("id", action.data.get(camel))
        where = "id = %(_target_id)s"
        params: dict[str, Any] = {"_target_id": target_id}
        if scope is not None:
            clause, scope_params = scope.sql_where(actor)
            where = f"{where} AND {clause}"
            params.update(scope_params)
        sql = f"SELECT * FROM {self._table(action.resource)} WHERE {where}"
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        return dict(row) if row is not None else None

    def cancel(self, handle: str) -> None:
        return None
