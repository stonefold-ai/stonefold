"""The dispatch worker (design §9, §8.4, §8.5).

Polls the outbox for ``PENDING`` rows, runs the locked ``PENDING → DISPATCHING``
transition (``claim_next_pending``) — re-evaluating the kill switch **inside** that
transaction (design §8.4, point 3) — dispatches the effect through the connector
with the row's **idempotency key**, and settles to ``DONE``/``FAILED``/``CANCELLED``
— writing the audit in the same transaction as the settle (invariant 6).

While a dispatch is in flight it is recorded in the ``InFlightRegistry`` so a kill
can abort a cancellable call (design §8.5); a connector that aborts raises
``ConnectorCancelled`` and the row settles ``CANCELLED``. A ``FAILED`` *irreversible*
effect with a declared compensation auto-stages the compensating effect
(design §9, RFC §8.5).

The worker depends only on ``acp_core`` protocols (``OutboxStore``,
``ConnectorRegistry``, ``Registry``, ``KillStore``) — the concretes are injected.
"""

from __future__ import annotations

from acp_core.audit import build_record
from acp_core.connector import ConnectorCancelled, ConnectorRegistry
from acp_core.enums import Decision, Reversibility
from acp_core.kill import KillStore, KillTarget
from acp_core.models import AuditRecord, EvalResult, RawCall, ResolvedAction, Session
from acp_core.outbox import KillCheck, OutboxStore, PendingAction, PendingState
from acp_core.registry import Registry
from acp_store.inflight import InFlightCall, InFlightRegistry


class DispatchWorker:
    """Drains staged effects exactly once (at-least-once dispatch + idempotency).

    The settle audit is written by the ``OutboxStore`` (in the same transaction
    as the state change, invariant 6); the worker only *builds* the record, so it
    needs no audit sink of its own.
    """

    def __init__(
        self,
        store: OutboxStore,
        connectors: ConnectorRegistry,
        *,
        registry: Registry | None = None,
        kill: KillStore | None = None,
        inflight: InFlightRegistry | None = None,
    ) -> None:
        self._store = store
        self._connectors = connectors
        self._registry = registry
        self._kill = kill
        self._inflight = inflight

    def _default_kill_check(self, row: PendingAction) -> bool:
        # The authoritative, in-transaction kill re-check (design §8.4). Reads the
        # injected kill store; ``None`` store ⇒ no check (M4 behaviour).
        assert self._kill is not None
        return self._kill.matches(KillTarget.from_pending(row)) is not None

    def run_once(self, kill_check: KillCheck | None = None) -> bool:
        """Process at most one staged row. Returns ``True`` if one was handled."""
        check: KillCheck | None = kill_check
        if check is None and self._kill is not None:
            check = self._default_kill_check

        claimed = self._store.claim_next_pending(check)
        if claimed is None:
            # either nothing PENDING, or the row was cancelled by the kill check
            return False

        try:
            connector = self._connectors.get(claimed.resolved.connector)
        except Exception as exc:  # unknown connector ⇒ fail closed
            self._store.settle(
                claimed.id,
                state=PendingState.FAILED,
                result={"error": str(exc)},
                audit=self._audit_record(claimed, Decision.DENY, "failure"),
            )
            self._maybe_compensate(claimed)
            return True

        call: InFlightCall | None = None
        if self._inflight is not None:
            call = InFlightCall(
                handle=claimed.idempotency_key,
                connector=connector,
                target=KillTarget.from_pending(claimed),
                action_id=claimed.id,
            )
            self._inflight.register(call)

        try:
            result = connector.dispatch(
                claimed.resolved, claimed.actor, claimed.idempotency_key
            )
        except ConnectorCancelled as exc:  # in-flight kill abort (design §8.5)
            self._store.settle(
                claimed.id,
                state=PendingState.CANCELLED,
                result={"cancelled": str(exc)},
                audit=self._audit_record(claimed, Decision.HALT, "cancelled"),
            )
            return True
        except Exception as exc:  # connector failure ⇒ settle FAILED, audit it
            self._store.settle(
                claimed.id,
                state=PendingState.FAILED,
                result={"error": str(exc)},
                audit=self._audit_record(claimed, Decision.DENY, "failure"),
            )
            self._maybe_compensate(claimed)
            return True
        finally:
            if call is not None and self._inflight is not None:
                self._inflight.unregister(call.handle)

        self._store.settle(
            claimed.id,
            state=PendingState.DONE,
            result=result.receipt,
            audit=self._audit_record(claimed, Decision.ALLOW, "success"),
        )
        return True

    def drain(self, *, max_iterations: int = 1000, kill_check: KillCheck | None = None) -> int:
        """Process pending rows until none remain (or the safety cap is hit)."""
        processed = 0
        while processed < max_iterations and self.run_once(kill_check):
            processed += 1
        return processed

    def _maybe_compensate(self, failed: PendingAction) -> None:
        """Auto-stage the declared compensation for a failed irreversible effect
        (design §9). No-op if reversible or no compensation declared."""
        comp = failed.compensation
        if comp is None or failed.resolved.attrs.reversibility is not Reversibility.IRREVERSIBLE:
            return
        comp_resolved = self._resolve_compensation(failed)
        self._store.stage(
            resolved=comp_resolved,
            actor=failed.actor,
            session_id=failed.session_id,
            agent=failed.agent,
            state=PendingState.PENDING,
            correlation_id=failed.correlation_id,
        )

    def _resolve_compensation(self, failed: PendingAction) -> ResolvedAction:
        comp = failed.compensation
        assert comp is not None  # guarded by caller
        if self._registry is not None:
            return self._registry.resolve(
                RawCall(resource=comp.resource, action=comp.action, data=dict(comp.data))
            )
        # No registry: synthesise a reversible compensating action that reuses the
        # parent's connector (enough for the worker to dispatch it).
        return failed.resolved.model_copy(
            update={
                "resource": comp.resource,
                "action": comp.action,
                "data": dict(comp.data),
                "attrs": failed.resolved.attrs.model_copy(
                    update={"reversibility": Reversibility.COMPENSABLE}
                ),
                "compensation": None,
            }
        )

    def _audit_record(self, row: PendingAction, decision: Decision, outcome: str) -> AuditRecord:
        result = EvalResult(decision=decision, rule="dispatch", gates=row.gates, ticket=row.id)
        return build_record(
            agent=row.agent,
            actor=row.actor,
            session=Session(id=row.session_id, correlation_id=row.correlation_id),
            call=RawCall(
                resource=row.resolved.resource,
                action=row.resolved.action,
                data=dict(row.resolved.data),
            ),
            resolved=row.resolved,
            result=result,
            outcome=outcome,
        )
