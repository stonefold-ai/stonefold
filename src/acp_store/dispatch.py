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

from collections.abc import Callable
from datetime import datetime, timezone

from acp_core.audit import build_record
from acp_core.digest import DIGEST_MISMATCH, digest_matches
from acp_core.connector import (
    SCOPE_LOST,
    ConnectorCancelled,
    ConnectorRegistry,
    ConnectorResult,
    ScopeLostError,
    ScopeReassertion,
    TransactionalDispatch,
    scope_capability_of,
)
from acp_core.enums import Decision, Reversibility
from acp_core.freshness import STALE_DECISION, DispatchRevalidator, stale_guard_reason
from acp_core.kill import KillStore, KillTarget
from acp_core.models import AuditRecord, EvalResult, RawCall, ResolvedAction, Session
from acp_core.outbox import KillCheck, OutboxStore, PendingAction, PendingState
from acp_core.registry import Registry
from acp_core.scope import ScopePredicate, ScopeResolver
from acp_store.inflight import InFlightCall, InFlightRegistry


def _result_refs_of(result: ConnectorResult) -> list[str]:
    """The downstream id(s) of a settled effect (RFC §11 ``resultRefs``, CS-009):
    the connector's explicit ``result_refs``, else ``[receipt['id']]`` if present,
    else ``[]``. The handle(s) an external system uses to locate/compensate it; a
    list because one dispatch may fan out to several records."""
    if result.result_refs:
        return list(result.result_refs)
    receipt = result.receipt
    if receipt is not None and receipt.get("id") is not None:
        return [str(receipt["id"])]
    return []


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
        clock: Callable[[], datetime] | None = None,
        revalidate: DispatchRevalidator | None = None,
        scopes: ScopeResolver | None = None,
    ) -> None:
        self._store = store
        self._connectors = connectors
        self._registry = registry
        self._kill = kill
        self._inflight = inflight
        self._clock = clock
        self._revalidate = revalidate
        # CS-018 scope no-race, opt-in like freshness: with a resolver the worker
        # re-asserts the scope predicate at dispatch — inside the effect's own
        # transaction for a transactional connector, via a pre-dispatch target
        # re-resolve for a window connector. ``None`` = v0.3 behaviour.
        self._scopes = scopes

    def _default_kill_check(self, row: PendingAction) -> bool:
        # The authoritative, in-transaction kill re-check (design §8.4). Reads the
        # injected kill store; ``None`` store ⇒ no check (M4 behaviour).
        assert self._kill is not None
        return self._kill.matches(KillTarget.from_pending(row)) is not None

    def _stale_check(self, row: PendingAction) -> str | None:
        # Decision freshness inside the claim (v0.4 CS-017), after the kill check:
        # TTL first, then the volatile-gate re-run. The worker is I/O-layer code,
        # so a wall clock is the default; tests/gateways inject their own.
        now = self._clock() if self._clock is not None else datetime.now(timezone.utc)
        if row.expires_at is not None and now >= row.expires_at:
            return STALE_DECISION
        if self._revalidate is not None:
            failing = self._revalidate(row, now)
            if failing is not None:
                return stale_guard_reason(failing.gate)
        return None

    def run_once(self, kill_check: KillCheck | None = None) -> bool:
        """Process at most one staged row. Returns ``True`` if one was handled."""
        check: KillCheck | None = kill_check
        if check is None and self._kill is not None:
            check = self._default_kill_check

        claimed = self._store.claim_next_pending(check, self._stale_check)
        if claimed is None:
            # nothing PENDING, or the remaining rows were cancelled in-claim
            # (kill / stale-decision / stale-guard)
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

        # CS-020: the pinned connector's artifact must still match its digest at
        # dispatch. A mismatch is a fail-closed dependency failure — the effect
        # never leaves, and since it never landed nothing is compensated (the same
        # floor as scope-lost: "authorized state or not at all").
        if claimed.resolved.connector_digest is not None and not digest_matches(
            connector, claimed.resolved.connector_digest
        ):
            self._settle_digest_failure(claimed)
            return True

        # CS-018 scope no-race (B4/B5): with a resolver wired, re-assert the scope
        # predicate at dispatch. The connector's declared capability picks the
        # form; either way the audit records which one ran.
        scope: ScopePredicate | None = None
        if self._scopes is not None:
            scope = self._scopes.scope_for(claimed.resolved.resource)
        scope_trace: tuple[str, ...] = ()
        txn: TransactionalDispatch | None = None
        if scope is not None:
            cap = scope_capability_of(connector)
            scope_trace = (f"{claimed.resolved.resource}:{scope.name}", cap.audit_note())
            if cap.reassertion is ScopeReassertion.TRANSACTIONAL:
                if not isinstance(connector, TransactionalDispatch):
                    # declared transactional but cannot carry the predicate into
                    # the effect's transaction ⇒ fail closed, nothing dispatched.
                    self._settle_scope_failure(claimed, "scope-unavailable", scope_trace)
                    return True
                txn = connector
            else:
                # window connector (B5): re-resolve the target under scope right
                # before the call, shrinking the race to connector latency.
                try:
                    target = connector.fetch_target(claimed.resolved, scope, claimed.actor)
                except Exception:  # cannot verify scope ⇒ fail closed (invariant 7)
                    self._settle_scope_failure(claimed, "scope-unavailable", scope_trace)
                    return True
                if target is None:
                    self._settle_scope_failure(claimed, SCOPE_LOST, scope_trace)
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
            if txn is not None and scope is not None:
                result = txn.dispatch_scoped(
                    claimed.resolved, claimed.actor, claimed.idempotency_key, scope
                )
            else:
                result = connector.dispatch(
                    claimed.resolved, claimed.actor, claimed.idempotency_key
                )
        except ScopeLostError:  # B4: the re-asserted predicate selected zero rows
            self._settle_scope_failure(claimed, SCOPE_LOST, scope_trace)
            return True
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
            audit=self._audit_record(
                claimed, Decision.ALLOW, "success", result_refs=_result_refs_of(result),
                scope_applied=scope_trace,
            ),
        )
        return True

    def _settle_scope_failure(
        self, row: PendingAction, reason: str, scope_trace: tuple[str, ...]
    ) -> None:
        """Settle a row whose scope could not be re-asserted at dispatch (CS-018).

        Never stages a compensation: a scope failure means the effect did NOT
        land ("authorized state or not at all"), so there is nothing to undo.
        """
        self._store.settle(
            row.id,
            state=PendingState.FAILED,
            result={"error": reason},
            reason=reason,
            audit=self._audit_record(
                row, Decision.DENY, "failure", rule=reason, scope_applied=scope_trace,
            ),
        )

    def _settle_digest_failure(self, row: PendingAction) -> None:
        """Settle a row whose connector failed digest verification at dispatch
        (CS-020). Never stages a compensation: the connector was never called, so
        the effect did not land and there is nothing to undo."""
        self._store.settle(
            row.id,
            state=PendingState.FAILED,
            result={"error": DIGEST_MISMATCH},
            reason=DIGEST_MISMATCH,
            audit=self._audit_record(row, Decision.DENY, "failure", rule=DIGEST_MISMATCH),
        )

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

    def _audit_record(
        self, row: PendingAction, decision: Decision, outcome: str,
        *, result_refs: list[str] | None = None, rule: str = "dispatch",
        scope_applied: tuple[str, ...] = (),
    ) -> AuditRecord:
        result = EvalResult(
            decision=decision, rule=rule, gates=row.gates, ticket=row.id,
            scope_applied=scope_applied,
        )
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
            result_refs=result_refs,
        )
