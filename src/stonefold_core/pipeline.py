"""The enforcement pipeline (RFC §12, design §3).

This is the spine: one ``enforce`` call per attempted action, always ending in an
audited terminal decision. The function is **pure and total** — no LLM, no
nondeterminism inside the decision logic (invariant 1).

The pipeline is split into two phases so a SIF batch can be decided atomically
(RFC §12, CS-023):

* ``_decide`` runs steps 1–5 (resolve → authorize → scope → gates → kill) and
  produces the operation's verdict **without committing anything** — no staging,
  no connector call, no audit write.
* ``_commit`` performs step 6/7 for one decided operation: stage a held or
  allowed effect, execute an allowed read/write, and write the audit record.

``enforce`` composes them once (single operation — the pre-batch behaviour,
unchanged). ``enforce_batch`` decides **every** operation first; any DENY or
HALT refuses the whole batch before anything commits or stages, otherwise every
operation commits in submission order (a HOLD stages ``PENDING_APPROVAL`` and
does not refuse the batch).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from datetime import datetime

from stonefold_core.audit import AuditSink, build_record
from stonefold_core.compiler import CompiledPolicy
from stonefold_core.connector import ConnectorRegistry
from stonefold_core.digest import DIGEST_MISMATCH, pinned_connector_mismatch
from stonefold_core.enums import Decision, Kind, Outcome
from stonefold_core.failure import Unavailable, guard, should_fail_closed
from stonefold_core.freshness import FreshnessConfig
from stonefold_core.gating import ApprovalSpec, GateEngine, ReleaseContract, RequestEnv
from stonefold_core.kill import KillStore, KillTarget
from stonefold_core.models import (
    Actor,
    BatchResult,
    EvalResult,
    GateResult,
    RawCall,
    ResolvedAction,
    Session,
)
from stonefold_core.outbox import OutboxStore, PendingState
from stonefold_core.policy import FailureMode
from stonefold_core.reasons import classify
from stonefold_core.registry import Registry, UnknownActionError
from stonefold_core.scope import ScopePredicate, ScopeResolver

# The audit ``outcome`` for an operation that was individually decided
# ALLOW/HOLD but never committed because another operation refused its batch
# (CS-023: every operation gets its own audit record; the batch refusal is
# visible on each).
BATCH_REFUSED = "batch-refused"


@dataclass(frozen=True)
class _Decided:
    """One operation's steps-1–5 verdict, before anything commits (CS-023).

    For a DENY/HALT the decision is terminal as-is; for ALLOW/HOLD the commit
    phase stages/executes it. Nothing has been staged, executed, or audited
    when this object exists.
    """

    call: RawCall
    resolved: ResolvedAction | None
    decision: Decision
    rule: str
    outcome: str = "not_executed"  # audit outcome if refused terminal
    gate_results: tuple[GateResult, ...] = ()
    approval: ApprovalSpec | None = None
    # v0.6 (CS-027): one release contract per holding gate; all must be satisfied.
    releases: tuple[ReleaseContract, ...] = ()
    scope_pred: ScopePredicate | None = None
    scope_applied: tuple[str, ...] = ()


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
    failure_mode = (
        policy.policy.defaults.failureMode if policy is not None else FailureMode.CLOSED
    )
    decided = _decide(
        call, actor, session, registry=registry, policy=policy, gates=gates,
        env=env, scopes=scopes, connectors=connectors, kill=kill,
        agent_name=agent_name, failure_mode=failure_mode,
    )
    return _commit(
        decided, actor, session, audit=audit, connectors=connectors,
        outbox=outbox, freshness=freshness, env=env, agent_name=agent_name,
        failure_mode=failure_mode,
    )


def enforce_batch(
    calls: Sequence[RawCall],
    actor: Actor,
    session: Session,
    *,
    registry: Registry,
    audit: AuditSink,
    policy: CompiledPolicy | None = None,
    gates: GateEngine | None = None,
    envs: Sequence[RequestEnv | None] | None = None,
    scopes: ScopeResolver | None = None,
    connectors: ConnectorRegistry | None = None,
    outbox: OutboxStore | None = None,
    kill: KillStore | None = None,
    freshness: FreshnessConfig | None = None,
    agent: str = "unknown",
) -> BatchResult:
    """Evaluate a SIF batch atomically (RFC §12, CS-023; SIF §5).

    Every operation is decided first (steps 1–5, each getting its own audit
    record); any DENY or HALT refuses the **whole batch** before anything
    commits or stages — the refused batch's other operations are audited with
    their own decision and outcome ``batch-refused``. A HOLD does **not**
    refuse the batch: the held effect stages ``PENDING_APPROVAL`` and the
    remaining operations commit alongside it. ``envs`` supplies the per-request
    environment for each operation, aligned by index (``None`` entries are
    legal — same meaning as ``enforce`` with no ``env``).

    A SIF batch has at least one operation (``sif.schema.json`` ``minItems``);
    an empty ``calls`` is a caller bug, not a policy decision.
    """
    if not calls:
        raise ValueError("a SIF batch carries at least one operation (SIF §5)")
    if envs is not None and len(envs) != len(calls):
        raise ValueError("envs must align with calls, one entry per operation")

    agent_name = policy.agent if policy is not None else agent
    failure_mode = (
        policy.policy.defaults.failureMode if policy is not None else FailureMode.CLOSED
    )
    env_of = (lambda i: envs[i]) if envs is not None else (lambda i: None)

    # Phase 1 — decide every operation (steps 1–5). Nothing commits or stages.
    decided = [
        _decide(
            call, actor, session, registry=registry, policy=policy, gates=gates,
            env=env_of(i), scopes=scopes, connectors=connectors, kill=kill,
            agent_name=agent_name, failure_mode=failure_mode,
        )
        for i, call in enumerate(calls)
    ]

    failing = next(
        (i for i, d in enumerate(decided) if d.decision in (Decision.DENY, Decision.HALT)),
        None,
    )
    if failing is not None:
        # Phase 2a — refuse the whole batch (CS-023): no record/transition
        # applies, no effect stages. Each operation still gets its own audit
        # record: refusals with their own rule/outcome, the rest with the
        # decision they earned and outcome ``batch-refused``.
        results = []
        for d in decided:
            if d.decision in (Decision.DENY, Decision.HALT):
                results.append(
                    _terminal(
                        d.decision, d.rule, d.call, d.resolved, actor, session,
                        audit, agent_name, gate_results=d.gate_results,
                        scope_applied=d.scope_applied, outcome=d.outcome,
                    )
                )
            else:
                results.append(
                    _terminal(
                        d.decision, d.rule, d.call, d.resolved, actor, session,
                        audit, agent_name, gate_results=d.gate_results,
                        scope_applied=d.scope_applied, outcome=BATCH_REFUSED,
                    )
                )
        return BatchResult(
            decision=decided[failing].decision,
            failing_index=failing,
            results=tuple(results),
        )

    # Phase 2b — commit: stage every hold/effect, execute every read/write, in
    # submission order. Per §4.4 the record ops commit atomically with the
    # staging — the in-memory reference approximates that shared transaction by
    # committing sequentially after the all-operations decision above; the
    # SQL-class connector binds them in one database transaction.
    # STONEFOLD-AMBIGUITY: RFC §12/CS-023 defines batch atomicity for the
    # *decision*; a dependency failure mid-commit (outbox/connector down after
    # earlier operations committed) is governed per-operation by §10 and is not
    # rolled back here.
    results = [
        _commit(
            d, actor, session, audit=audit, connectors=connectors, outbox=outbox,
            freshness=freshness, env=env_of(i), agent_name=agent_name,
            failure_mode=failure_mode,
        )
        for i, d in enumerate(decided)
    ]
    commit_failure = next(
        (i for i, r in enumerate(results) if r.decision in (Decision.DENY, Decision.HALT)),
        None,
    )
    if commit_failure is not None:
        return BatchResult(
            decision=results[commit_failure].decision,
            failing_index=commit_failure,
            results=tuple(results),
        )
    decision = (
        Decision.HOLD
        if any(r.decision is Decision.HOLD for r in results)
        else Decision.ALLOW
    )
    return BatchResult(decision=decision, failing_index=None, results=tuple(results))


def _decide(
    call: RawCall,
    actor: Actor,
    session: Session,
    *,
    registry: Registry,
    policy: CompiledPolicy | None,
    gates: GateEngine | None,
    env: RequestEnv | None,
    scopes: ScopeResolver | None,
    connectors: ConnectorRegistry | None,
    kill: KillStore | None,
    agent_name: str,
    failure_mode: FailureMode,
) -> _Decided:
    """Steps 1–5 for one operation (RFC §12) — the verdict, nothing committed."""

    # 1. RESOLVE (RFC §12 step 1) — done *first* so every terminal record, even a
    # halt or a refusal, carries the resolved kind/resource/action the audit
    # requires (RFC §11). An unknown name short-circuits to DENY before any policy
    # runs.
    resolved: ResolvedAction | None
    try:
        resolved = registry.resolve(call)
    except UnknownActionError:
        return _Decided(call, None, Decision.DENY, "unknown-action")

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
            return _Decided(
                call, resolved, Decision.HALT, f"kill:{pre_order.id}", outcome="halted"
            )

    # No policy loaded ⇒ nothing is explicitly allowed ⇒ default deny (M0).
    if policy is None:
        return _Decided(call, resolved, Decision.DENY, "default-deny")

    # 2. AUTHORIZE — RFC §6.2: deny-wins → default-deny → allow.
    authz = policy.authorize(resolved)
    if not authz.allowed:
        return _Decided(call, resolved, Decision.DENY, authz.rule)

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
                        return _Decided(
                            call, resolved, Decision.DENY, "scope-unavailable",
                            scope_applied=scope_applied,
                        )
                elif probe.value is None:
                    return _Decided(
                        call, resolved, Decision.DENY, "scope-denied",
                        scope_applied=scope_applied,
                    )

    # 4. GATES — evaluate the matching gates (RFC §7/§12 step 4). Any FAIL ⇒
    # DENY (short-circuited before approvals); else any HOLD ⇒ HOLD (staged at
    # commit).
    gate_trace: tuple[GateResult, ...] = ()
    if gates is not None:
        outcome = gates.evaluate(resolved, actor, session, policy, env or RequestEnv())
        if outcome.outcome is Outcome.FAIL:
            return _Decided(
                call, resolved, Decision.DENY, outcome.reason or "gate-fail",
                gate_results=outcome.results, scope_pred=scope_pred,
                scope_applied=scope_applied,
            )
        if outcome.outcome is Outcome.HOLD:
            return _Decided(
                call, resolved, Decision.HOLD, outcome.reason or "gate-hold",
                gate_results=outcome.results, approval=outcome.approval,
                releases=outcome.releases,
                scope_pred=scope_pred, scope_applied=scope_applied,
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
                return _Decided(
                    call, resolved, Decision.HALT, "kill-unavailable",
                    outcome="halted", gate_results=gate_trace,
                    scope_pred=scope_pred, scope_applied=scope_applied,
                )
            order = None
        else:
            order = kill_probe.value
        if order is not None:
            return _Decided(
                call, resolved, Decision.HALT, f"kill:{order.id}",
                outcome="halted", gate_results=gate_trace,
                scope_pred=scope_pred, scope_applied=scope_applied,
            )

    return _Decided(
        call, resolved, Decision.ALLOW, authz.rule, gate_results=gate_trace,
        scope_pred=scope_pred, scope_applied=scope_applied,
    )


def _commit(
    decided: _Decided,
    actor: Actor,
    session: Session,
    *,
    audit: AuditSink,
    connectors: ConnectorRegistry | None,
    outbox: OutboxStore | None,
    freshness: FreshnessConfig | None,
    env: RequestEnv | None,
    agent_name: str,
    failure_mode: FailureMode,
) -> EvalResult:
    """Step 6/7 for one decided operation: stage/execute, then audit (RFC §12)."""

    call, resolved = decided.call, decided.resolved

    # A steps-1–5 refusal is terminal as-is.
    if decided.decision in (Decision.DENY, Decision.HALT):
        return _terminal(
            decided.decision, decided.rule, call, resolved, actor, session, audit,
            agent_name, gate_results=decided.gate_results,
            scope_applied=decided.scope_applied, outcome=decided.outcome,
        )

    assert resolved is not None  # ALLOW/HOLD always carries the resolved action

    if decided.decision is Decision.HOLD:
        # A HOLD suspends the action: stage it as PENDING_APPROVAL so a human
        # can release it later (design §7). The ticket is returned to the agent.
        ticket = None
        if outbox is not None:
            ob = outbox
            expiry = _staging_expiry(freshness, env, resolved)
            if isinstance(expiry, Unavailable):
                return _terminal(
                    Decision.DENY, "freshness-unavailable", call, resolved, actor,
                    session, audit, agent_name, gate_results=decided.gate_results,
                    scope_applied=decided.scope_applied,
                )
            expires_at: datetime | None = expiry
            held = guard(
                lambda: ob.stage(
                    resolved=resolved, actor=actor, session_id=session.id,
                    agent=agent_name, state=PendingState.PENDING_APPROVAL,
                    correlation_id=session.correlation_id,
                    gates=decided.gate_results, approval=decided.approval,
                    releases=decided.releases,
                    expires_at=expires_at,
                ),
                reason="outbox-unavailable",
            )
            if isinstance(held, Unavailable):
                # can't durably suspend the action ⇒ fail closed (design §11/§12).
                return _terminal(
                    Decision.DENY, "outbox-unavailable", call, resolved, actor,
                    session, audit, agent_name, gate_results=decided.gate_results,
                    scope_applied=decided.scope_applied,
                )
            ticket = held.value.id
        return _terminal(
            Decision.HOLD, decided.rule, call, resolved, actor, session, audit,
            agent_name, gate_results=decided.gate_results, ticket=ticket,
            scope_applied=decided.scope_applied,
            approval=_approval_audit(decided.approval, ticket, decided.releases),
        )

    # 6. EXECUTE (decision is ALLOW).
    # Effects are staged via the outbox by default (invariant 4): on ALLOW we
    # write a PENDING row and return an accepted/pending receipt — the dispatch
    # worker sends it (design §9). Without an outbox the effect is not dispatched.
    gate_trace = decided.gate_results
    if resolved.kind is Kind.EFFECT:
        if outbox is not None:
            ob = outbox
            expiry = _staging_expiry(freshness, env, resolved)
            if isinstance(expiry, Unavailable):
                return _terminal(
                    Decision.DENY, "freshness-unavailable", call, resolved, actor,
                    session, audit, agent_name, gate_results=gate_trace,
                    scope_applied=decided.scope_applied,
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
                    scope_applied=decided.scope_applied,
                )
            return _terminal(
                Decision.ALLOW, decided.rule, call, resolved, actor, session, audit,
                agent_name, gate_results=gate_trace,
                scope_applied=decided.scope_applied,
                ticket=staged.value.id, outcome="staged",
            )
        return _terminal(
            Decision.ALLOW, decided.rule, call, resolved, actor, session, audit,
            agent_name, gate_results=gate_trace, scope_applied=decided.scope_applied,
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
                    scope_applied=decided.scope_applied,
                )
            return _terminal(
                Decision.ALLOW, decided.rule, call, resolved, actor, session, audit,
                agent_name, gate_results=gate_trace,
                scope_applied=decided.scope_applied,
            )
        executed = guard(
            lambda: connectors.get(resolved.connector).execute(
                resolved, decided.scope_pred, actor
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
                    scope_applied=decided.scope_applied,
                )
            return _terminal(
                Decision.ALLOW, decided.rule, call, resolved, actor, session, audit,
                agent_name, gate_results=gate_trace,
                scope_applied=decided.scope_applied,
            )
        cresult = executed.value
        output: Any = cresult.rows if cresult.kind == "rows" else cresult.receipt
        return _terminal(
            Decision.ALLOW, decided.rule, call, resolved, actor, session, audit,
            agent_name, gate_results=gate_trace, scope_applied=decided.scope_applied,
            output=output, outcome="success",
        )

    return _terminal(
        Decision.ALLOW, decided.rule, call, resolved, actor, session, audit,
        agent_name, gate_results=gate_trace, scope_applied=decided.scope_applied,
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
    spec: "ApprovalSpec | None",
    ticket: str | None,
    releases: tuple[ReleaseContract, ...] = (),
) -> dict[str, Any] | None:
    """Render the release contract(s) for the audit record (RFC §11 ``approval``).

    A held action records *who* may release it and the quorum/timeout terms, with
    a ``pending`` status — the eventual approver(s)/resolver(s) and outcome are
    written when the row settles. The legacy top-level keys mirror the first
    contract (pre-v0.6 consumers); ``releases`` lists EVERY holding gate's
    contract (CS-027), each with its cause, reason code, and evidence (I7).
    ``None`` when nothing holds the action.
    """
    if spec is None and not releases:
        return None
    rendered: dict[str, Any] = {"status": "pending", "ticket": ticket}
    if spec is not None:
        rendered.update(
            {
                "quorum": spec.quorum,
                "dualAuthorization": spec.dual_auth,
                "distinctFromActor": spec.distinct_from_actor,
                "approvers": list(spec.approvers),
                "timeoutSeconds": spec.timeout_s,
                "onTimeout": spec.on_timeout,
            }
        )
    if releases:
        rendered["releases"] = [contract.audit_dict() for contract in releases]
    return rendered


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

    reason_code, retry_class = classify(decision, rule, gate_results)
    result = EvalResult(
        decision=decision,
        rule=rule,
        gates=gate_results,
        reason_code=reason_code,
        retry_class=retry_class,
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
