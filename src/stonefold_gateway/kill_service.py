"""The operator-facing kill service (design §8.6–§8.7, RFC §9).

Issuing or lifting a kill is itself an **audited operator action** (who/when/scope,
design §8.2). On issue the service also drives the two defense-in-depth steps the
design lists beyond the chokepoint (design §8.7): it cancels any in-flight
cancellable connector calls the kill matches (design §8.5), optionally calls the
agent runtime's cancel API, and optionally revokes the downstream connector
credentials. The hooks are protocols with no-op-able implementations — a real
deployment wires its runtime/secret-manager here.

This is the gateway wiring layer; the durable kill state and the matcher live in
``stonefold_core``/``stonefold_store``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Protocol

from stonefold_core.audit import AuditSink
from stonefold_core.enums import Decision
from stonefold_core.kill import KillOrder, KillScope, KillScopeKind, KillStore, order_matches
from stonefold_core.models import AuditRecord
from stonefold_store.inflight import InFlightRegistry


class RuntimeCancel(Protocol):
    """Stops the agent's LLM loop / revokes its session token (design §8.7)."""

    def cancel_session(self, session_id: str) -> None: ...


class CredentialRevoke(Protocol):
    """Rotates/disables the downstream connector credentials so even a code bug
    can't dispatch after a kill (design §8.7)."""

    def revoke(self, scope: KillScope) -> None: ...


def _now() -> datetime:
    return datetime.now(timezone.utc)


class KillService:
    def __init__(
        self,
        store: KillStore,
        *,
        audit: AuditSink | None = None,
        inflight: InFlightRegistry | None = None,
        runtime_cancel: RuntimeCancel | None = None,
        credential_revoke: CredentialRevoke | None = None,
    ) -> None:
        self._store = store
        self._audit = audit
        self._inflight = inflight
        self._runtime_cancel = runtime_cancel
        self._credential_revoke = credential_revoke

    def issue(
        self, scope: KillScope, *, issued_by: str, predicate: str | None = None
    ) -> KillOrder:
        order = self._store.issue(scope, issued_by=issued_by, predicate=predicate)
        self._audit_operator_action("kill.issue", order, issued_by)

        # §8.5 — abort in-flight cancellable calls this order matches.
        if self._inflight is not None:
            self._inflight.cancel_matching(lambda call: order_matches(order, call.target))

        # §8.7 — defense in depth beyond the chokepoint (best-effort).
        if (
            self._runtime_cancel is not None
            and scope.kind is KillScopeKind.SESSION
            and scope.session_id is not None
        ):
            self._runtime_cancel.cancel_session(scope.session_id)
        if self._credential_revoke is not None:
            self._credential_revoke.revoke(scope)
        return order

    def lift(self, order_id: str, *, lifted_by: str) -> KillOrder:
        order = self._store.lift(order_id)
        self._audit_operator_action("kill.lift", order, lifted_by)
        return order

    def active(self) -> tuple[KillOrder, ...]:
        return self._store.active_orders()

    def _audit_operator_action(self, action: str, order: KillOrder, actor: str) -> None:
        if self._audit is None:
            return
        self._audit.write(
            AuditRecord(
                id=f"audit_{uuid.uuid4().hex[:12]}",
                timestamp=_now(),
                agent="operator",
                actor=actor,
                action=action,
                parameters={
                    "order_id": order.id,
                    "scope": order.scope.model_dump(mode="json"),
                    "predicate": order.predicate,
                },
                decision=Decision.HALT,
                outcome="halted",
            )
        )
