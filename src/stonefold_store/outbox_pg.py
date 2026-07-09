"""Postgres ``OutboxStore`` (design §9, §8.4; invariants 4–6).

The ``pending_actions`` table is the durable substrate. ``claim_next_pending``
runs the **locked** ``PENDING → DISPATCHING`` transition as a single
``SELECT … FOR UPDATE SKIP LOCKED`` transaction, so a row is claimed by at most
one worker — the property SQLite can't demonstrate, which is why the invariants
mandate Postgres here. ``settle`` writes the audit row in the *same* transaction
as the state change (invariant 6).

The whole ``PendingAction`` is stored as JSONB (re-serialised on each transition);
denormalised ``state``/``seq`` columns drive the claim query. ``psycopg`` is
imported lazily so importing this module never requires it.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from stonefold_core.gating import ApprovalSpec, ReleaseContract
from stonefold_core.models import Actor, AuditRecord, Compensation, GateResult, ResolvedAction
from stonefold_core.outbox import (
    ApprovalError,
    KillCheck,
    PendingAction,
    PendingState,
    StaleCheck,
    UnknownTicketError,
    apply_release,
    cancellation_record,
)
from stonefold_store.outbox_memory import build_pending

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_actions (
    seq            bigserial PRIMARY KEY,
    id             text UNIQUE NOT NULL,
    idempotency_key text UNIQUE NOT NULL,
    state          text NOT NULL,
    agent          text NOT NULL,
    actor_id       text NOT NULL,
    session_id     text,
    correlation_id text,
    payload        jsonb NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS pending_actions_state_seq ON pending_actions (state, seq);
CREATE TABLE IF NOT EXISTS audit_log (
    seq        bigserial,
    id         text PRIMARY KEY,
    record     jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS audit_log_correlation
    ON audit_log ((record->>'correlationId'), seq);
"""


def create_schema(conn: Any) -> None:
    with conn.cursor() as cur:
        cur.execute(_SCHEMA)


def _to_payload(row: PendingAction) -> Any:
    from psycopg.types.json import Jsonb

    return Jsonb(row.model_dump(mode="json"))


def _from_payload(payload: Any) -> PendingAction:
    data = payload if isinstance(payload, dict) else json.loads(payload)
    return PendingAction.model_validate(data)


class PostgresOutboxStore:
    def __init__(self, conn: Any, audit_table: str = "audit_log") -> None:
        self._conn = conn
        self._audit_table = audit_table

    def stage(
        self,
        *,
        resolved: ResolvedAction,
        actor: Actor,
        session_id: str,
        agent: str,
        state: PendingState,
        correlation_id: str | None = None,
        gates: tuple[GateResult, ...] = (),
        approval: ApprovalSpec | None = None,
        releases: tuple[ReleaseContract, ...] = (),
        compensation: Compensation | None = None,
        expires_at: datetime | None = None,
    ) -> PendingAction:
        row = build_pending(
            resolved=resolved, actor=actor, session_id=session_id, agent=agent,
            state=state, correlation_id=correlation_id, gates=gates,
            approval=approval, releases=releases, compensation=compensation,
            expires_at=expires_at,
        )
        with self._conn.transaction(), self._conn.cursor() as cur:
            cur.execute(
                """INSERT INTO pending_actions
                   (id, idempotency_key, state, agent, actor_id, session_id,
                    correlation_id, payload)
                   VALUES (%(id)s, %(key)s, %(state)s, %(agent)s, %(actor_id)s,
                           %(session_id)s, %(correlation_id)s, %(payload)s)""",
                {
                    "id": row.id,
                    "key": row.idempotency_key,
                    "state": row.state.value,
                    "agent": row.agent,
                    "actor_id": row.actor.id,
                    "session_id": row.session_id,
                    "correlation_id": row.correlation_id,
                    "payload": _to_payload(row),
                },
            )
        return row

    def get(self, action_id: str) -> PendingAction | None:
        with self._conn.cursor() as cur:
            cur.execute("SELECT payload FROM pending_actions WHERE id = %s", (action_id,))
            found = cur.fetchone()
        return _from_payload(found[0]) if found is not None else None

    def list_by_state(self, state: PendingState) -> list[PendingAction]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT payload FROM pending_actions WHERE state = %s ORDER BY seq",
                (state.value,),
            )
            rows = cur.fetchall()
        return [_from_payload(r[0]) for r in rows]

    def claim_next_pending(
        self,
        kill_check: KillCheck | None = None,
        stale_check: StaleCheck | None = None,
    ) -> PendingAction | None:
        # Each iteration claims the next PENDING row in its own FOR UPDATE
        # transaction; a killed/stale row is cancelled in that same transaction
        # (§8.4 / CS-017) and the scan moves on so it never blocks the queue.
        while True:
            with self._conn.transaction(), self._conn.cursor() as cur:
                cur.execute(
                    """SELECT id, payload FROM pending_actions
                       WHERE state = %s ORDER BY seq
                       FOR UPDATE SKIP LOCKED LIMIT 1""",
                    (PendingState.PENDING.value,),
                )
                found = cur.fetchone()
                if found is None:
                    return None
                action_id, payload = found
                row = _from_payload(payload)
                if kill_check is not None and kill_check(row):
                    # killed inside the SAME transaction as the claim (§8.4).
                    cancelled = row.model_copy(
                        update={"state": PendingState.CANCELLED, "reason": "kill"}
                    )
                    self._write(cur, cancelled)
                    return None
                if stale_check is not None and (stale := stale_check(row)) is not None:
                    # stale inside the claim (v0.4 CS-017): cancel + audit commit
                    # together, then scan for the next fresh row.
                    cancelled = row.model_copy(
                        update={"state": PendingState.CANCELLED, "reason": stale}
                    )
                    self._write(cur, cancelled)
                    self._write_audit(cur, cancellation_record(cancelled, stale))
                    continue
                claimed = row.model_copy(update={"state": PendingState.DISPATCHING})
                self._write(cur, claimed)
                return claimed

    def settle(
        self,
        action_id: str,
        *,
        state: PendingState,
        result: dict[str, Any] | None = None,
        audit: AuditRecord | None = None,
        reason: str | None = None,
    ) -> PendingAction:
        with self._conn.transaction(), self._conn.cursor() as cur:
            row = self._lock(cur, action_id)
            settled = row.model_copy(update={"state": state, "result": result, "reason": reason})
            self._write(cur, settled)
            if audit is not None:
                self._write_audit(cur, audit)  # same transaction as the settle
        return settled

    def approve(
        self, action_id: str, approver_id: str, *, gate: str | None = None
    ) -> PendingAction:
        with self._conn.transaction(), self._conn.cursor() as cur:
            row = self._lock(cur, action_id)
            # CS-027: shared, pure release logic — every contract must be
            # satisfied; the FOR UPDATE lock serialises concurrent releases.
            updated = apply_release(row, approver_id, gate=gate)
            self._write(cur, updated)
        return updated

    def reject(self, action_id: str, approver_id: str) -> PendingAction:
        with self._conn.transaction(), self._conn.cursor() as cur:
            row = self._lock(cur, action_id)
            if row.state is not PendingState.PENDING_APPROVAL:
                raise ApprovalError(f"{action_id} is {row.state.value}, not awaiting approval")
            updated = row.model_copy(
                update={"state": PendingState.CANCELLED, "reason": f"rejected by {approver_id}"}
            )
            self._write(cur, updated)
        return updated

    # --- helpers ---------------------------------------------------------
    def _lock(self, cur: Any, action_id: str) -> PendingAction:
        cur.execute(
            "SELECT payload FROM pending_actions WHERE id = %s FOR UPDATE", (action_id,)
        )
        found = cur.fetchone()
        if found is None:
            raise UnknownTicketError(action_id)
        return _from_payload(found[0])

    def _write(self, cur: Any, row: PendingAction) -> None:
        cur.execute(
            """UPDATE pending_actions
               SET state = %(state)s, payload = %(payload)s, updated_at = now()
               WHERE id = %(id)s""",
            {"state": row.state.value, "payload": _to_payload(row), "id": row.id},
        )

    def _write_audit(self, cur: Any, audit: AuditRecord) -> None:
        from psycopg.types.json import Jsonb

        cur.execute(
            f"INSERT INTO {self._audit_table} (id, record) VALUES (%s, %s)",
            (audit.id, Jsonb(audit.model_dump(mode="json"))),
        )
