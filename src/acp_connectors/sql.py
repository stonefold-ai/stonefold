"""SQL connector (design §5) — the canonical "scope injected below the model".

The agent's intent is translated to SQL; the connector then **appends** the
scope predicate as an extra ``WHERE`` clause (``AND owner_id = %(scope_owner_id)s``)
that the agent never named and cannot widen. It is the reference *transactional*
connector (CS-018): a registered effect statement carries the predicate inside
the effect's own transaction, so the write lands on authorized state or not at
all. ``psycopg`` is imported lazily so the package loads without it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from acp_core.connector import ConnectorResult, ScopeCapability, ScopeLostError
from acp_core.enums import Kind
from acp_core.models import Actor, ResolvedAction
from acp_core.scope import ScopePredicate

_PARAM_RE = re.compile(r"%\((\w+)\)s")


class SqlConnector:
    """Reads/records against Postgres with the scope filter appended below the
    gateway. ``conn`` is a live ``psycopg.Connection``.

    ``effect_sql`` registers the statement each staged effect dispatches to,
    keyed ``"Resource.action"``. A template is plain SQL with ``%(name)s``
    parameters filled from the action's ``data``, plus a literal ``{scope}``
    slot where the scope predicate's constraint is ANDed in (CS-018) — e.g.::

        UPDATE accounts SET balance = balance - %(amount)s
        WHERE id = %(accountId)s AND {scope}
    """

    scope_capability = ScopeCapability.transactional()

    def __init__(
        self,
        conn: Any,
        *,
        table_map: dict[str, str] | None = None,
        effect_sql: Mapping[str, str] | None = None,
    ) -> None:
        self._conn = conn
        self._table_map = dict(table_map or {})
        self._effect_sql = dict(effect_sql or {})
        # process-local retry dedupe; a real deployment keys a dedupe table on
        # the idempotency key so the guarantee survives restarts (design §9).
        self._dispatched: dict[str, ConnectorResult] = {}

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
        template = self._effect_sql.get(f"{action.resource}.{action.action}")
        if template is None:
            # no registered effect statement (pre-CS-018 stub behaviour): record
            # a receipt keyed by the idempotency key.
            return ConnectorResult(kind="receipt", receipt={"sent": True}, handle=idempotency_key)
        return self._dispatch(template, action, actor, idempotency_key, None)

    def dispatch_scoped(
        self, action: ResolvedAction, actor: Actor, idempotency_key: str,
        scope: ScopePredicate,
    ) -> ConnectorResult:
        """CS-018 transactional dispatch: the scope predicate is ANDed into the
        effect's own write. Zero rows affected ⇒ ``ScopeLostError`` and the
        transaction rolls back — never a partial or un-authorized commit."""
        template = self._effect_sql.get(f"{action.resource}.{action.action}")
        if template is None:
            # declared transactional but no statement to carry the predicate
            # into ⇒ a dependency failure; the worker fails closed.
            raise RuntimeError(
                f"no effect statement registered for {action.resource}.{action.action}"
            )
        return self._dispatch(template, action, actor, idempotency_key, scope)

    def _dispatch(
        self, template: str, action: ResolvedAction, actor: Actor,
        idempotency_key: str, scope: ScopePredicate | None,
    ) -> ConnectorResult:
        if idempotency_key in self._dispatched:
            # at-least-once retry: return the prior result, never re-apply.
            return self._dispatched[idempotency_key]
        clause, scope_params = ("1 = 1", {}) if scope is None else scope.sql_where(actor)
        sql = template.replace("{scope}", clause)
        params: dict[str, Any] = {
            name: action.data[name] for name in _PARAM_RE.findall(sql) if name in action.data
        }
        params.update(scope_params)
        with self._conn.transaction(), self._conn.cursor() as cur:
            cur.execute(sql, params)
            affected = int(cur.rowcount)
            if scope is not None and affected == 0:
                # the re-asserted predicate no longer selects the target: raising
                # here rolls the transaction back (B4 — "authorized state or not
                # at all", the same shape as the kill no-race).
                raise ScopeLostError(
                    f"{action.resource}.{action.action}: scope predicate "
                    f"{scope.name!r} no longer selects the target"
                )
        result = ConnectorResult(
            kind="receipt",
            receipt={"applied": affected > 0, "rows": affected},
            query=sql,
            handle=idempotency_key,
        )
        self._dispatched[idempotency_key] = result
        return result

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
