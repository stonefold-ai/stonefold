"""The enforcement pipeline (RFC §12, design §3).

This is the spine: one ``enforce`` call per attempted action, always ending in an
audited terminal decision. The function is **pure and total** — no LLM, no
nondeterminism inside the decision logic (invariant 1).

Implemented through M1: step 1 (resolve) and step 2 (authorize: default-deny →
deny-wins → most-specific allow). Scope (M3), gates (M2), kill (M5), and
execution/outbox (M4) slot in at the marked points. When no ``policy`` is
supplied the function behaves as the M0 stub (default-deny everything).
"""

from __future__ import annotations

from typing import Any

from datetime import datetime

from stonefold_core.audit import AuditSink, build_record
from stonefold_core.compiler import CompiledPolicy
from stonefold_core.connector import ConnectorRegistry
from stonefold_core.digest import DIGEST_MISMATCH, pinned_connector_mismatch
from stonefold_core.enums import Decision, Kind, Outcome
from stonefold_core.failure import Unavailable, guard, should_fail_closed
from stonefold_core.freshness import FreshnessConfig
from stonefold_core.gating import ApprovalSpec, GateEngine, RequestEnv
from stonefold_core.kill import KillStore, KillTarget
from stonefold_core.models import (
    Actor,
    EvalResult,
    GateResult,
    RawCall,
    ResolvedAction,
    Session,
)
from stonefold_core.outbox import OutboxStore, PendingState
from stonefold_core.registry import Registry, UnknownActionError
from stonefold_core.scope import ScopePredicate, ScopeResolver


def enforce(
    call: RawCall,
    actor: Actor,
    session: Session,
    *,
    registry: Registry,
    audit: AuditSink,
    policy: CompiledPolicy | None = None,
    gates: GateEngine | None = None,
    env: RequestEnv | None = None,
    scopes: ScopeResolver | None = None,
    connectors: ConnectorRegistry | None = None,
    outbox: OutboxStore | None = None,
    kill: KillStore | None = None,
    freshness: FreshnessConfig | None = None,
    agent: str = "unknown",
) -> EvalResult:
    """Evaluate one attempted action to a terminal, audited decision.

    Every return path writes exactly one audit record (RFC §11) via
    ``_terminal``. The stages run in the strict RFC §12 order, stopping at the
    first terminal verdict. Stages whose dependency is not injected are skipped:
    no ``gates`` ⇒ authorization alone decides (M1); no ``connectors`` ⇒ an
    allowed non-effect is not executed (M2 behaviour).
    """

    agent_name = policy.agent if policy is not None else agent

    # 1. RESOLVE (RFC §12 step 1) — done *first* so every terminal record, even a
    # halt or a refusal, carries the resolved kind/resource/action the audit
    # requires (RFC §11). An unknown name short-circuits to DENY before any policy
    # runs.
    resolved: ResolvedAction | None
    try:
        resolved = registry.resolve(call)
    except UnknownActionError:
        return _terminal(
            Decision.DENY, "unknown-action", call, None, actor, session, audit, agent_name
        )

    # 0. KILL pre-check (design §8.3 point 1): short-circuit a fully-killed
    # global/agent/session before the policy/scope/gate work. ACTION_CLASS orders
    # need the resolved kind, so they are matched at step 5 below — not here. A
    # store error is swallowed and the fail-closed decision deferred to step 5
    # (where the kind, hence the irreversible-effect rule, is known).
    if kill is not None:
        pre_target = KillTarget(agent=agent_name, session_id=session.id)
        try:
            pre_order = kill.matches(pre_target)
        except Exception:
            pre_order = None
        if pre_order is not None:
            return _terminal(
                Decision.HALT, f"kill:{pre_order.id}", call, resolved, actor,
                session, audit, agent_name, outcome="halted",
            )

    # No policy loaded ⇒ nothing is explicitly allowed ⇒ default deny (M0).
    if policy is None:
        return _terminal(
            Decision.DENY, "default-deny", call, resolved, actor, session, audit, agent_name
        )

    # 2. AUTHORIZE — RFC §6.2: deny-wins → default-deny → allow.
    authz = policy.authorize(resolved)
    if not authz.allowed:
        return _terminal(
            Decision.DENY, authz.rule, call, resolved, actor, session, audit, agent_name
        )

    # The policy's failure mode governs every dependency-failure branch below
    # (RFC §10, design §12). ``should_fail_closed`` applies it, with the
    # irreversible-effect floor.
    failure_mode = policy.policy.defaults.failureMode

    # 3. SCOPE — derive the predicate from the actor (never the payload). For an
    # effect this is a pre-resolution authorization check (design §5): the target
    # must be visible under scope, else DENY before any dispatch.
    scope_pred: ScopePredicate | None = None
    scope_applied: tuple[str, ...] = ()
    if scopes is not None:
        scope_pred = scopes.scope_for(resolved.resource)
        if scope_pred is not None:
            scope_applied = (f"{resolved.resource}:{scope_pred.name}",)
            if resolved.kind is Kind.EFFECT and connectors is not None:
                probe = guard(
                    lambda: connectors.get(resolved.connector).fetch_target(
                        resolved, scope_pred, actor
                    ),
                    reason="scope-unavailable",
                )
                if isinstance(probe, Unavailable):
                    # dependency failure ⇒ honour failureMode (invariant 7). Open
                    # skips the scope pre-check; closed (and any irreversible) denies.
                    if should_fail_closed(resolved, failure_mode):
                        return _terminal(
                            Decision.DENY, "scope-unavailable", call, resolved, actor,
                            session, audit, agent_name, scope_applied=scope_applied,
                        )
                elif probe.value is None:
                    return _terminal(
                        Decision.DENY, "scope-denied", call, resolved, actor,
                        session, audit, agent_name, scope_applied=scope_applied,
                    )

    # 4. GATES — evaluate the matching gates (RFC §7/§12 step 4). Any FAIL ⇒
    # DENY (short-circuited before approvals); else any HOLD ⇒ HOLD.
    gate_trace: tuple[GateResult, ...] = ()
    if gates is not None:
        outcome = gates.evaluate(resolved, actor, session, policy, env or RequestEnv())
        if outcome.outcome is Outcome.FAIL:
            return _terminal(
                Decision.DENY, outcome.reason or "gate-fail", call, resolved,
                actor, session, audit, agent_name,
                gate_results=outcome.results, scope_applied=scope_applied,
            )
        if outcome.outcome is Outcome.HOLD:
            # A HOLD suspends the action: stage it as PENDING_APPROVAL so a human
            # can release it later (design §7). The ticket is returned to the agent.
            ticket = None
            if outbox is not None:
                ob = outbox
                expiry = _staging_expiry(freshness, env, resolved)
                if isinstance(expiry, Unavailable):
                    return _terminal(
                        Decision.DENY, "freshness-unavailable", call, resolved, actor,
                        session, audit, agent_name, gate_results=outcome.results,
                        scope_applied=scope_applied,
                    )
                expires_at: datetime | None = expiry
                held = guard(
                    lambda: ob.stage(
                        resolved=resolved, actor=actor, session_id=session.id,
                        agent=agent_name, state=PendingState.PENDING_APPROVAL,
                        correlation_id=session.correlation_id,
                        gates=outcome.results, approval=outcome.approval,
                        expires_at=expires_at,
                    ),
                    reason="outbox-unavailable",
                )
                if isinstance(held, Unavailable):
                    # can't durably suspend the action ⇒ fail closed (design §11/§12).
                    return _terminal(
                        Decision.DENY, "outbox-unavailable", call, resolved, actor,
                        session, audit, agent_name, gate_results=outcome.results,
                        scope_applied=scope_applied,
                    )
                ticket = held.value.id
            return _terminal(
                Decision.HOLD, outcome.reason or "gate-hold", call, resolved,
                actor, session, audit, agent_name,
                gate_results=outcome.results, ticket=ticket,
                scope_applied=scope_applied,
                approval=_approval_audit(outcome.approval, ticket),
            )
        gate_trace = outcome.results

    # 5. KILL — the chokepoint check (RFC §12 step 5, design §8.3 point 2). An
    # active kill of any scope (including ACTION_CLASS, matched here on the
    # resolved kind/resource/action) turns the action into an audited HALT — a
    # distinct terminal state, never staged. An *unreadable* kill fails closed:
    # an irreversible effect is halted unconditionally, anything else honours the
    # policy's failureMode (design §8.9, invariant 7).
    if kill is not None:
        kill_probe = guard(
            lambda: kill.matches(
                KillTarget.from_resolved(resolved, actor, session, agent_name)
            ),
            reason="kill-unavailable",
        )
        if isinstance(kill_probe, Unavailable):
            if should_fail_closed(resolved, failure_mode):
                return _terminal(
                    Decision.HALT, "kill-unavailable", call, resolved, actor,
                    session, audit, agent_name, gate_results=gate_trace,
                    scope_applied=scope_applied, outcome="halted",
                )
            order = None
        else:
            order = kill_probe.value
        if order is not None:
            return _terminal(
                Decision.HALT, f"kill:{order.id}", call, resolved, actor,
                session, audit, agent_name, gate_results=gate_trace,
                scope_applied=scope_applied, outcome="halted",
            )

    # 6. EXECUTE.
    # Effects are staged via the outbox by default (invariant 4): on ALLOW we
    # write a PENDING row and return an accepted/pending receipt — the dispatch
    # worker sends it (design §9). Without an outbox the effect is not dispatched.
    if resolved.kind is Kind.EFFECT:
        if outbox is not None:
            ob = outbox
            expiry = _staging_expiry(freshness, env, resolved)
            if isinstance(expiry, Unavailable):
                return _terminal(
                    Decision.DENY, "freshness-unavailable", call, resolved, actor,
                    session, audit, agent_name, gate_results=gate_trace,
                    scope_applied=scope_applied,
                )
            effect_expires_at: datetime | None = expiry
            staged = guard(
                lambda: ob.stage(
                    resolved=resolved, actor=actor, session_id=session.id,
                    agent=agent_name, state=PendingState.PENDING,
                    correlation_id=session.correlation_id,
                    gates=gate_trace, compensation=resolved.compensation,
                    expires_at=effect_expires_at,
                ),
                reason="outbox-unavailable",
            )
            if isinstance(staged, Unavailable):
                # the durable staging+evidence substrate is down. We can neither
                # stage, approve, nor record the effect, so failureMode 'open' does
                # not apply here — always fail closed (design §11/§12, invariant 7).
                return _terminal(
                    Decision.DENY, "outbox-unavailable", call, resolved, actor,
                    session, audit, agent_name, gate_results=gate_trace,
                    scope_applied=scope_applied,
                )
            return _terminal(
                Decision.ALLOW, authz.rule, call, resolved, actor, session, audit,
                agent_name, gate_results=gate_trace, scope_applied=scope_applied,
                ticket=staged.value.id, outcome="staged",
            )
        return _terminal(
            Decision.ALLOW, authz.rule, call, resolved, actor, session, audit,
            agent_name, gate_results=gate_trace, scope_applied=scope_applied,
        )

    # observe/record/transition run through the connector now, scope applied
    # below the model (design §5).
    if connectors is not None:
        # CS-020: a pinned connector whose loaded artifact no longer matches its
        # digest is a dependency failure (RFC §10) — honour failureMode, with the
        # irreversible floor, exactly like a connector outage below.
        if pinned_connector_mismatch(connectors, resolved):
            if should_fail_closed(resolved, failure_mode):
                return _terminal(
                    Decision.DENY, DIGEST_MISMATCH, call, resolved, actor,
                    session, audit, agent_name, gate_results=gate_trace,
                    scope_applied=scope_applied,
                )
            return _terminal(
                Decision.ALLOW, authz.rule, call, resolved, actor, session, audit,
                agent_name, gate_results=gate_trace, scope_applied=scope_applied,
            )
        executed = guard(
            lambda: connectors.get(resolved.connector).execute(
                resolved, scope_pred, actor
            ),
            reason="connector-unavailable",
        )
        if isinstance(executed, Unavailable):
            # connector/dependency failure ⇒ honour failureMode (RFC §10). Closed
            # denies; open allows the read through with no output (low-stakes).
            if should_fail_closed(resolved, failure_mode):
                return _terminal(
                    Decision.DENY, "connector-unavailable", call, resolved, actor,
                    session, audit, agent_name, gate_results=gate_trace,
                    scope_applied=scope_applied,
                )
            return _terminal(
                Decision.ALLOW, authz.rule, call, resolved, actor, session, audit,
                agent_name, gate_results=gate_trace, scope_applied=scope_applied,
            )
        cresult = executed.value
        output: Any = cresult.rows if cresult.kind == "rows" else cresult.receipt
        return _terminal(
            Decision.ALLOW, authz.rule, call, resolved, actor, session, audit,
            agent_name, gate_results=gate_trace, scope_applied=scope_applied,
            output=output, outcome="success",
        )

    return _terminal(
        Decision.ALLOW, authz.rule, call, resolved, actor, session, audit,
        agent_name, gate_results=gate_trace, scope_applied=scope_applied,
    )


def _staging_expiry(
    freshness: FreshnessConfig | None,
    env: RequestEnv | None,
    resolved: ResolvedAction,
) -> datetime | None | Unavailable:
    """The ``expires_at`` to stamp on a row being staged (v0.4 CS-017).

    ``None`` when freshness is not configured (opt-in: pre-v0.4 behaviour).
    Freshness configured but no injected clock ⇒ ``Unavailable``: the gateway
    cannot bound the decision's validity, and CS-017 requires every staged row's
    TTL to be finite — so staging fails closed unconditionally (invariant 7),
    like the outbox itself being down.
    """
    if freshness is None:
        return None
    now = env.now if env is not None else None
    if now is None:
        return Unavailable(reason="freshness-unavailable")
    return freshness.expiry_for(resolved, now)


def _approval_audit(
    spec: "ApprovalSpec | None", ticket: str | None
) -> dict[str, Any] | None:
    """Render the approval contract for the audit record (RFC §11 ``approval``).

    A held action records *who* may release it and the quorum/timeout terms, with
    a ``pending`` status — the eventual approver(s) and outcome are written by the
    outbox when the row is approved/rejected. ``None`` when no approval applies.
    """
    if spec is None:
        return None
    return {
        "status": "pending",
        "ticket": ticket,
        "quorum": spec.quorum,
        "dualAuthorization": spec.dual_auth,
        "distinctFromActor": spec.distinct_from_actor,
        "approvers": list(spec.approvers),
        "timeoutSeconds": spec.timeout_s,
        "onTimeout": spec.on_timeout,
    }


def _terminal(
    decision: Decision,
    rule: str,
    call: RawCall,
    resolved: ResolvedAction | None,
    actor: Actor,
    session: Session,
    audit: AuditSink,
    agent: str,
    *,
    gate_results: tuple[GateResult, ...] = (),
    ticket: str | None = None,
    output: Any | None = None,
    scope_applied: tuple[str, ...] = (),
    outcome: str = "not_executed",
    approval: dict[str, Any] | None = None,
) -> EvalResult:
    """Build the terminal result, write its audit record, and return it.

    Centralising this guarantees invariant 6: no terminal path can forget to
    audit.
    """

    result = EvalResult(
        decision=decision,
        rule=rule,
        gates=gate_results,
        ticket=ticket,
        output=output,
        scope_applied=scope_applied,
    )
    audit.write(
        build_record(
            agent=agent,
            actor=actor,
            session=session,
            call=call,
            resolved=resolved,
            result=result,
            outcome=outcome,
            approval=approval,
        )
    )
    return result
