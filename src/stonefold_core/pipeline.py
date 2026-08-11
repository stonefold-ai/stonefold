# SPDX-License-Identifier: Apache-2.0
"""The enforcement pipeline (spec §12, design §3).

This is the spine: one ``enforce`` call per attempted action, always ending in an
audited terminal decision. The function is **pure and total** — no LLM, no
nondeterminism inside the decision logic (invariant 1).

The pipeline is split into two phases so a SIF batch can be decided atomically
(spec §12):

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

import json
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from datetime import datetime

from stonefold_core.audit import AuditSink, build_record
from stonefold_core.compiler import CompiledPolicy
from stonefold_core.connector import ConnectorRegistry
from stonefold_core.digest import DIGEST_MISMATCH, pinned_connector_mismatch
from stonefold_core.enums import (
    Coverage,
    Decision,
    EnforcementMode,
    FeedbackLevel,
    Kind,
    Outcome,
)
from stonefold_core.failure import Unavailable, guard, should_fail_closed
from stonefold_core.freshness import FreshnessConfig
from stonefold_core.gating import ApprovalSpec, GateEngine, ReleaseContract, RequestEnv
from stonefold_core.kill import KillStore, KillTarget
from stonefold_core.models import (
    Actor,
    Advice,
    AuditRecord,
    BatchResult,
    EvalResult,
    GateResult,
    ItemVerdict,
    RawCall,
    ResolvedAction,
    Session,
)
from stonefold_core.obligation import (
    Capability,
    ConsumeOutcome,
    ObligationClaim,
    ObligationRegistry,
    ReserveOutcome,
    claim_evidence,
)
from stonefold_core.outbox import (
    OutboxStore,
    PendingAction,
    PendingState,
    hold_dedupe_key,
    hold_identity,
)
from stonefold_core.policy import FailureMode
from stonefold_core.reasons import classify
from stonefold_core.registry import Registry, UnknownActionError
from stonefold_core.scope import ScopePredicate, ScopeResolver

# The audit ``outcome`` for an operation that was individually decided
# ALLOW/HOLD but never committed because another operation refused its batch
# (every operation gets its own audit record; the batch refusal is
# visible on each).
BATCH_REFUSED = "batch-refused"

# The rule an advisory allow presents to the ACTOR. The rule that actually
# decided is written to the audit record instead (``_AdvisoryAudit``): an actor
# that learns which rule would have stopped it has learned that nothing will,
# and the traffic being measured stops being ordinary traffic.
ADVISORY_RULE = "advisory"

# The rule for an item-bearing call whose item count is over its declared
# ceiling: refused without being evaluated, item by item or at all.
ITEMS_OVER_CEILING = "items-over-ceiling"

# How many returned rows an advisory deployment evaluates its scope predicate
# over before it stops and says so (D-A4). A wide read is the one place
# measurement has a real cost, and a sampled number presented as a census is
# exactly the dishonesty the report exists to avoid — so the cap is bounded and
# the record is marked partial, never silently truncated.
SCOPE_MEASURE_CAP = 10_000


@dataclass(frozen=True)
class ScopeMeasure:
    """A scope predicate an advisory deployment must MEASURE instead of apply.

    Applying it would give the agent fewer rows than it gets today, which
    changes what the estate does and stops advisory being advisory (D-A4). So
    the read runs unscoped and the predicate is evaluated over what came back,
    to count what it would have removed. Counts only, never values: the record
    must not become a copy of the rows the policy was trying to keep out of
    reach.
    """

    predicate: ScopePredicate
    label: str  # what ``scopeApplied`` would have said, had it been applied
    cap: int = SCOPE_MEASURE_CAP


def _measure_scope(measure: ScopeMeasure, output: Any, actor: Actor) -> dict[str, Any]:
    """Count what the scope predicate would have removed from an unscoped read.

    Never raises and never refuses: a measurement that failed is recorded as one
    (``measured: false`` with the reason), because an advisory deployment that
    turned a broken predicate into a refused read would be enforcing by
    accident — and a failed measurement silently reported as zero would be worse
    than no measurement at all.
    """
    if not isinstance(output, (list, tuple)):
        # a receipt, a scalar, nothing at all: there are no rows to narrow
        return {"predicate": measure.label, "measured": False, "reason": "output-not-rows"}
    rows = list(output)
    evaluated = rows[: measure.cap]
    removed = 0
    for row in evaluated:
        if not isinstance(row, Mapping):
            return {
                "predicate": measure.label, "measured": False,
                "reason": "rows-not-attributes",
            }
        try:
            in_scope = measure.predicate.matches(row, actor)
        except Exception:
            return {
                "predicate": measure.label, "measured": False,
                "reason": "predicate-raised",
            }
        if not in_scope:
            removed += 1
    return {
        "predicate": measure.label,
        "measured": True,
        "removed": removed,
        "evaluated": len(evaluated),
        "returned": len(rows),
        "partial": len(evaluated) < len(rows),
    }


def _kill_caused(rule: str) -> bool:
    """Whether a HALT came from the kill machinery — a matched order or an
    unreadable kill store.

    Advisory mode never translates these. A kill order is an operator pulling
    the cord, not a policy verdict, and an unreadable store means the gateway
    cannot know whether the cord has been pulled; the enforcing pipeline already
    fails closed there (step 5) and a watch-only deployment keeps that behaviour.
    It is the single lever an advisory deployment promises to keep, so it
    outranks transparency.
    """
    return rule == "kill-unavailable" or rule.startswith("kill:")


def _unscoped(decided: _Decided, cap: int) -> _Decided:
    """Move a scope predicate from APPLIED to MEASURED (D-A4).

    An advisory deployment that narrowed a read would hand the agent fewer rows
    than it gets today, and the traffic being measured would no longer be the
    estate's own. So the predicate stops being an argument to the connector and
    becomes something the commit phase counts afterwards. ``scope_applied`` is
    cleared with it: the record says what was applied, and nothing was.
    """
    if decided.scope_pred is None:
        return decided
    label = decided.scope_applied[0] if decided.scope_applied else decided.scope_pred.name
    return replace(
        decided,
        scope_pred=None,
        scope_applied=(),
        scope_measure=ScopeMeasure(decided.scope_pred, label, cap),
    )


def _as_advised(
    decided: _Decided, cap: int = SCOPE_MEASURE_CAP
) -> tuple[_Decided, Advice | None, Coverage]:
    """Translate one verdict for a deployment that does not enforce.

    Returns the decision to commit, the advice to record, and the coverage the
    record must carry. A refusal becomes an ALLOW carrying no approval and no
    release contract — so a held action stages nothing, which is the whole
    point: nobody is going to answer it and the effect has already happened.

    An ALLOW passes through untouched with no advice: the field marks
    divergence between what the policy said and what the deployment did, not
    the mode itself (the record's ``enforcement`` says the mode).

    Coverage is decided here because only the pre-translation verdict can say
    it: ``UNJUDGED`` for an unresolvable name and for a decide-phase dependency
    failure (a ``*-unavailable`` rule) — in both, what enforcement "would have
    done" is a fail-closed reflex, not a policy judgement, and counting it as
    judged would overstate the one number the report leads with.
    """
    if decided.decision is Decision.ALLOW:
        # No divergence in the verdict — but an applied scope is a divergence in
        # what the estate does, so it is measured rather than applied (D-A4).
        return _unscoped(decided, cap), None, Coverage.JUDGED
    if decided.decision is Decision.HALT and _kill_caused(decided.rule):
        # A kill halt is enforced for real, and it is a judgement: the operator
        # made it. ``kill-unavailable`` fails closed for real too (D-A2's
        # guarantee outranks transparency), so both stand untranslated.
        return decided, None, Coverage.JUDGED
    if decided.resolved is None:
        # An unresolvable name has no connector, so there is nothing to let
        # through: the gateway cannot forward what it cannot address. The deny
        # stands, marked as the coverage case — the gateway saw an action it
        # could not judge — never counted as an advisory allow.
        return decided, None, Coverage.UNJUDGED

    coverage = (
        Coverage.UNJUDGED
        if decided.rule.endswith("-unavailable")
        else Coverage.JUDGED
    )
    reason_code, retry_class = classify(
        decided.decision, decided.rule, decided.gate_results
    )
    # An advised hold stages nothing (D-A5), so this is the ONLY place its
    # question's identity is ever computed. Stamped from the same function the
    # live queue collapses on, so a report counting distinct questions and a
    # running gateway's queue cannot drift apart.
    identity = (
        hold_identity(decided.resolved, decided.gate_results)
        if decided.decision is Decision.HOLD and decided.resolved is not None
        else None
    )
    advice = Advice(
        decision=decided.decision,
        rule=decided.rule,
        reason_code=reason_code,
        retry_class=retry_class,
        dedupe_key=json.dumps(identity, default=str) if identity is not None else None,
    )
    return (
        _unscoped(
            replace(
                decided,
                decision=Decision.ALLOW,
                rule=ADVISORY_RULE,
                outcome="not_executed",
                approval=None,
                releases=(),
            ),
            cap,
        ),
        advice,
        coverage,
    )


class _AdvisoryAudit:
    """Stamps the advisory profile's fields onto every record written through it.

    Stamping the sink rather than branching inside ``_commit`` keeps the commit
    phase and every terminal path unchanged — advisory is one translation before
    the commit and one stamp after it, so an advisory deployment and an enforcing
    one run the same code and compute the same verdicts. That identity is what
    lets a report claim what enforcement *would* have done; see the TCK's
    advisory profile.

    The commit phase does carry the mode as a *label* (``_commit``'s
    ``enforcement`` argument), stamped onto the row it stages so the settle and
    dispatch records written later know which deployment they came from. Nothing
    there reads it to decide anything, which is what preserves the identity
    above.

    ``advice`` is set per operation before its commit (the pipeline is
    sequential and single-threaded, so a batch's operations each get their own).
    """

    def __init__(self, inner: AuditSink) -> None:
        self._inner = inner
        self.advice: Advice | None = None
        self.batch_advice: dict[str, Any] | None = None
        self.item_advice: dict[str, Any] | None = None
        self.coverage: Coverage = Coverage.JUDGED

    def write(self, record: AuditRecord) -> None:
        update: dict[str, Any] = {
            "enforcement": EnforcementMode.ADVISORY,
            "coverage": self.coverage,
        }
        if self.advice is not None:
            update["advised"] = self.advice
            # spec §11 wants the deciding rule on the record; the actor-facing
            # result carries ``ADVISORY_RULE`` instead. Only where the
            # translation actually went through, though: the commit phase can
            # still refuse for real (a dead outbox, a lost scope), and that
            # record's deciding rule is the failure's own — the would-have rule
            # stays in ``advised``, where divergence belongs.
            if record.decision is Decision.ALLOW:
                update["rule"] = self.advice.rule
        if self.batch_advice is not None:
            update["batchAdvice"] = self.batch_advice
        if self.item_advice is not None:
            update["itemAdvice"] = self.item_advice
        self._inner.write(record.model_copy(update=update))


@dataclass(frozen=True)
class _Decided:
    """One operation's steps-1–5 verdict, before anything commits.

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
    # v0.3: one release contract per holding gate; all must be satisfied.
    releases: tuple[ReleaseContract, ...] = ()
    scope_pred: ScopePredicate | None = None
    scope_applied: tuple[str, ...] = ()
    # advisory profile (D-A4): the predicate to MEASURE rather than apply. Set
    # by the translation, read by the commit phase — data, not a mode check, so
    # the commit phase still cannot tell which deployment it is running in.
    scope_measure: ScopeMeasure | None = None
    # v0.3: the policy-declared agent-feedback level, stamped on the
    # EvalResult so the transport can redact the return path.
    feedback: FeedbackLevel = FeedbackLevel.CODE_FIELDS
    # v0.3: a reservation taken by the BATCH pre-pass, so the commit
    # phase uses it instead of reserving again. ``None`` in the single-op path
    # (the commit reserves itself, just before staging).
    claim: ObligationClaim | None = None


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
    obligations: Mapping[str, ObligationRegistry] | None = None,
    dedupe_window_s: float | None = None,
    agent: str = "unknown",
    enforcement: EnforcementMode = EnforcementMode.ENFORCED,
    scope_measure_cap: int = SCOPE_MEASURE_CAP,
) -> EvalResult:
    """Evaluate one attempted action to a terminal, audited decision.

    Every return path writes exactly one audit record (spec §11) via
    ``_terminal``. The stages run in the strict spec §12 order, stopping at the
    first terminal verdict. Stages whose dependency is not injected are skipped:
    no ``gates`` ⇒ authorization alone decides (M1); no ``connectors`` ⇒ an
    allowed non-effect is not executed (M2 behaviour). ``obligations`` supplies
    the obligation-registry adapters the commit phase reserves/consumes from
    (v0.3) — the same map the gate engine matches against.
    """

    agent_name = policy.agent if policy is not None else agent
    failure_mode = (
        policy.policy.defaults.failureMode if policy is not None else FailureMode.CLOSED
    )
    # an action declaring independent items is decided item by item.
    # Only here, never inside ``enforce_batch``: a SIF batch applies atomically
    #, so an item-bearing action inside one stays a single unit.
    items_def = _items_declaration(registry, call)
    if items_def is not None:
        return _enforce_per_item(
            call, actor, session, items_def=items_def, registry=registry, audit=audit,
            policy=policy, gates=gates, env=env, scopes=scopes, connectors=connectors,
            outbox=outbox, kill=kill, freshness=freshness, obligations=obligations,
            dedupe_window_s=dedupe_window_s, agent_name=agent_name,
            failure_mode=failure_mode, enforcement=enforcement,
            scope_measure_cap=scope_measure_cap,
        )
    decided = _decide(
        call, actor, session, registry=registry, policy=policy, gates=gates,
        env=env, scopes=scopes, connectors=connectors, kill=kill,
        agent_name=agent_name, failure_mode=failure_mode,
    )
    if enforcement is EnforcementMode.ADVISORY:
        # The verdict above is computed and recorded exactly as it would be
        # under enforcement; only what happens next differs.
        advisory = _AdvisoryAudit(audit)
        decided, advisory.advice, advisory.coverage = _as_advised(
            decided, scope_measure_cap
        )
        audit = advisory
    result = _commit(
        decided, actor, session, audit=audit, connectors=connectors,
        outbox=outbox, freshness=freshness, env=env, agent_name=agent_name,
        failure_mode=failure_mode, obligations=obligations,
        dedupe_window_s=dedupe_window_s, enforcement=enforcement,
    )
    return _stamp_feedback(result, decided)


# --- item-bearing actions ----------------------------------------
def _items_declaration(registry: Registry, call: RawCall) -> Any | None:
    """The action's items declaration, or ``None`` if it is not item-bearing.

    Resolution is an O(1) lookup and ``_decide`` resolves again per item; paying
    it twice is cheaper than widening the ``Registry`` protocol every consumer
    implements. An unknown name resolves to ``None`` here and reaches the normal
    path, which is what turns it into the audited DENY (spec §12 step 1).
    """
    try:
        resolved = registry.resolve(call)
    except UnknownActionError:
        return None
    declared = resolved.items
    if declared is None or not getattr(declared, "independent", False):
        return None
    values = call.data.get(declared.field)
    # A single value, or none at all, has nothing to fan out: the ordinary path
    # decides it as one unit and says so in one record.
    if not isinstance(values, (list, tuple)) or len(values) < 2:
        return None
    return declared


def _item_key(value: Any, declared: Any) -> str:
    """How a refusal names the item it refused.

    Scalars name themselves. Objects need a declared ``key``, and without one the
    position is all there is — which is why the linter warns about an item-bearing
    action whose items are objects and whose key is undeclared.
    """
    key = getattr(declared, "key", "")
    if key and isinstance(value, Mapping):
        return str(value.get(key, ""))
    return str(value)


def _item_advice_entry(name: str, advice: Advice, coverage: Coverage) -> dict[str, Any]:
    """One item's line in ``itemAdvice``: what enforcement would have done to it."""
    entry: dict[str, Any] = {
        "item": name,
        "decision": advice.decision.value,
        "rule": advice.rule,
        "reasonCode": advice.reason_code,
    }
    if advice.retry_class is not None:
        entry["retryClass"] = advice.retry_class.value
    if coverage is Coverage.UNJUDGED:
        # this item's verdict was a fail-closed reflex, not a judgement of it
        entry["coverage"] = coverage.value
    return entry


def _call_advice(
    entries: Sequence[tuple[str, Advice | None, Coverage]],
) -> tuple[Advice | None, dict[str, Any] | None]:
    """The advice for the one record an advised item-bearing call writes.

    Every item is applied, so there is a single record where enforcement would
    have written one per refusal — and a single ``advised`` cannot hold N
    verdicts, the same way one operation's record cannot hold a batch's
    atomicity. So ``advised`` carries what the ACTOR would have been told
    (``_worst``, exactly as the enforcing envelope reports a partial
    application), and ``itemAdvice`` carries which items produced it.

    Items enforcement would have applied anyway get no entry: the field marks
    divergence, item by item, as ``advised`` does for the call. No divergence at
    all ⇒ both are ``None``, and the record is a plain advisory allow.
    """
    divergent = [(name, a, c) for name, a, c in entries if a is not None]
    if not divergent:
        return None, None
    worst = _worst(a.decision for _name, a, _c in divergent)
    return (
        next(a for _name, a, _c in divergent if a.decision is worst),
        {
            "wouldApply": len(entries) - len(divergent),
            "wouldRefuse": len(divergent),
            "items": [_item_advice_entry(n, a, c) for n, a, c in divergent],
        },
    )


def _worst(decisions: Iterable[Decision]) -> Decision:
    """The call's fate as a whole: ALLOW only if every item was applied.

    The order is the refusal's finality for the actor: HALT means an operator
    stopped it, DENY means it will not happen, HOLD means it might.
    """
    seen = set(decisions)
    for decision in (Decision.HALT, Decision.DENY, Decision.HOLD):
        if decision in seen:
            return decision
    return Decision.ALLOW


def _enforce_per_item(
    call: RawCall,
    actor: Actor,
    session: Session,
    *,
    items_def: Any,
    registry: Registry,
    audit: AuditSink,
    policy: CompiledPolicy | None,
    gates: GateEngine | None,
    env: RequestEnv | None,
    scopes: ScopeResolver | None,
    connectors: ConnectorRegistry | None,
    outbox: OutboxStore | None,
    kill: KillStore | None,
    freshness: FreshnessConfig | None,
    obligations: Mapping[str, ObligationRegistry] | None,
    dedupe_window_s: float | None,
    agent_name: str,
    failure_mode: FailureMode,
    enforcement: EnforcementMode = EnforcementMode.ENFORCED,
    scope_measure_cap: int = SCOPE_MEASURE_CAP,
) -> EvalResult:
    """Decide an item-bearing action item by item.

    Where each part of the answer comes from, and why:

    * **every gate runs per item**, which is the entire point — an aggregate gate
      (``spendLimit``/``rate``/``quota``) now sees N increments and can cut the run
      off at item seven, which is *these seven, not those thirteen*;
    * **the applied items go through as one call**, because that is one transaction
      in the managed system, and splitting it into N would misdescribe what
      happened;
    * **held items stage individually**, so a human releases one item rather than
      the whole call;
    * **refused items are each audited on their own**, because each is its own
      decision with its own reason code, and that record is the only place the
      refusal exists.

    Under advisory the fan-out and every verdict are computed identically — the
    property the report rests on — and then each item's verdict is translated by
    the §2 seam, so the whole call is applied as one. The refusals that advisory
    does not translate (an operator's kill, an item the gateway cannot address)
    still get their own mode-stamped record; the rest are recorded on the applied
    call's record as ``itemAdvice``. An advised hold stages nothing, which falls
    out of the translation: it commits as an ALLOW carrying no approval.
    """
    advisory: _AdvisoryAudit | None = None
    if enforcement is EnforcementMode.ADVISORY:
        # One wrapper for the whole call. The pipeline is sequential, so the
        # advice and coverage set before a write are the ones that write earned.
        advisory = _AdvisoryAudit(audit)
        audit = advisory

    values = list(call.data[items_def.field])
    names = [_item_key(value, items_def) for value in values]
    ceiling = getattr(items_def, "maxItems", None)
    if ceiling is not None and len(values) > ceiling:
        if advisory is None:
            # Refusing on size is honest; quietly evaluating fifty thousand items
            # is not. One record, one reason, nothing applied.
            return _terminal(
                Decision.DENY, ITEMS_OVER_CEILING, call, None, actor, session,
                audit, agent_name,
            )
        # An advisory deployment refuses nothing but a kill order (D-A2), and a
        # ceiling refusal is not one — a second silent refusal would break the
        # promise the customer routed their traffic on. So the call goes through
        # as one unfanned unit. The verdict that unit reaches is NOT the
        # counterfactual (enforcement would have refused on the ceiling, without
        # looking), so the ceiling refusal is what ``advised`` carries, and
        # coverage says the items themselves were never judged.
        whole = _decide(
            call, actor, session, registry=registry, policy=policy, gates=gates,
            env=env, scopes=scopes, connectors=connectors, kill=kill,
            agent_name=agent_name, failure_mode=failure_mode,
        )
        forwarded, _unused, coverage = _as_advised(whole, scope_measure_cap)
        if forwarded.decision is Decision.ALLOW:
            reason_code, retry_class = classify(Decision.DENY, ITEMS_OVER_CEILING, ())
            advisory.advice = Advice(
                decision=Decision.DENY, rule=ITEMS_OVER_CEILING,
                reason_code=reason_code, retry_class=retry_class,
            )
            advisory.coverage = Coverage.UNJUDGED
        else:
            # A kill halt, or a name the gateway cannot address: the refusal
            # stands, and ``_as_advised`` has already said which coverage it is.
            advisory.coverage = coverage
        return _stamp_feedback(
            _commit(
                forwarded, actor, session, audit=advisory, connectors=connectors,
                outbox=outbox, freshness=freshness, env=env, agent_name=agent_name,
                failure_mode=failure_mode, obligations=obligations,
                dedupe_window_s=dedupe_window_s,
                enforcement=EnforcementMode.ADVISORY,
            ),
            forwarded,
        )

    decided = [
        _decide(
            RawCall(
                resource=call.resource,
                action=call.action,
                data={**call.data, items_def.field: [value]},
            ),
            actor, session, registry=registry, policy=policy, gates=gates,
            env=env, scopes=scopes, connectors=connectors, kill=kill,
            agent_name=agent_name, failure_mode=failure_mode,
        )
        for value in values
    ]

    # Advisory translates each item's verdict AFTER every item is decided, so
    # the decide phase — and therefore the verdicts themselves — is bit-identical
    # to an enforcing run of the same traffic.
    advices: list[Advice | None] = [None] * len(decided)
    coverages: list[Coverage] = [Coverage.JUDGED] * len(decided)
    if advisory is not None:
        for i, d in enumerate(decided):
            decided[i], advices[i], coverages[i] = _as_advised(d, scope_measure_cap)

    verdicts: list[ItemVerdict] = []
    applied: list[str] = []
    allowed: list[tuple[int, _Decided]] = []

    for index, d in enumerate(decided):
        name = names[index]
        if advisory is not None:
            # whatever this iteration writes, it is this item's record
            advisory.advice = advices[index]
            advisory.coverage = coverages[index]
        if d.decision in (Decision.DENY, Decision.HALT):
            refused = _terminal(
                d.decision, d.rule, d.call, d.resolved, actor, session, audit,
                agent_name, gate_results=d.gate_results,
                scope_applied=d.scope_applied, outcome=d.outcome,
            )
            verdicts.append(ItemVerdict(
                item=name, decision=refused.decision, rule=refused.rule,
                reason_code=refused.reason_code, retry_class=refused.retry_class,
            ))
        elif d.decision is Decision.HOLD:
            held = _stamp_feedback(_commit(
                d, actor, session, audit=audit, connectors=connectors, outbox=outbox,
                freshness=freshness, env=env, agent_name=agent_name,
                failure_mode=failure_mode, obligations=obligations,
                dedupe_window_s=dedupe_window_s, enforcement=enforcement,
            ), d)
            verdicts.append(ItemVerdict(
                item=name, decision=held.decision, rule=held.rule,
                reason_code=held.reason_code, retry_class=held.retry_class,
                ticket=held.ticket,
            ))
        else:
            allowed.append((index, d))
            verdicts.append(ItemVerdict(item=name, decision=Decision.ALLOW, rule=d.rule))
            applied.append(name)

    merged: EvalResult | None = None
    if allowed:
        # One call for the allowed subset. The template is the first allowed
        # item's verdict: the gates that ran are the same set for every item, and
        # where a gate's *values* differed, that difference is recorded on the
        # items it refused rather than here.
        template = allowed[0][1]
        if advisory is not None:
            # Under advisory the subset includes items enforcement would have
            # refused, and the template's gate results are what the commit acts
            # on (an obligation reservation, a freshness window). Prefer an item
            # enforcement would have allowed too, so the merged call commits the
            # way an enforcing deployment would have committed its applied
            # subset. All-refused leaves the first item, which is all there is.
            template = next(
                (d for i, d in allowed if advices[i] is None), allowed[0][1]
            )
            advisory.advice, advisory.item_advice = _call_advice(
                [(names[i], advices[i], coverages[i]) for i, _ in allowed]
            )
            # One unjudged item makes the call's coverage unjudged: the record
            # covers the whole call, and understating our own reach is the only
            # safe direction to round. The per-item entries keep which it was.
            advisory.coverage = (
                Coverage.UNJUDGED
                if any(coverages[i] is Coverage.UNJUDGED for i, _ in allowed)
                else Coverage.JUDGED
            )
        subset = [values[i] for i, _ in allowed]
        assert template.resolved is not None
        merged_decided = replace(
            template,
            call=RawCall(
                resource=call.resource,
                action=call.action,
                data={**call.data, items_def.field: subset},
            ),
            resolved=template.resolved.model_copy(
                update={"data": {**template.resolved.data, items_def.field: subset}}
            ),
        )
        merged = _stamp_feedback(_commit(
            merged_decided, actor, session, audit=audit, connectors=connectors,
            outbox=outbox, freshness=freshness, env=env, agent_name=agent_name,
            failure_mode=failure_mode, obligations=obligations,
            dedupe_window_s=dedupe_window_s, enforcement=enforcement,
        ), template)
        if merged.decision is not Decision.ALLOW:
            # The commit phase refused what the decide phase allowed — a
            # dispatch-time re-validation, a digest mismatch. Nothing was applied,
            # so the items say that rather than keeping the decide-phase verdict.
            applied = []
            verdicts = [
                ItemVerdict(
                    item=v.item, decision=merged.decision, rule=merged.rule,
                    reason_code=merged.reason_code, retry_class=merged.retry_class,
                    ticket=merged.ticket,
                ) if v.decision is Decision.ALLOW else v
                for v in verdicts
            ]
        elif advisory is not None:
            # The applied items went through as one call, so they carry the
            # call's answer — one rule for all of them. Item by item they would
            # differ (``ADVISORY_RULE`` where a verdict was translated, the
            # ordinary allow rule where none was), and an actor reading them
            # side by side would learn exactly which items the policy dislikes:
            # an enumeration oracle, delivered per call. The call-level
            # disclosure stands (a translated verdict answers ``advisory``); what
            # is withheld is WHICH item earned it.
            verdicts = [
                v.model_copy(update={"rule": merged.rule})
                if v.decision is Decision.ALLOW else v
                for v in verdicts
            ]

    overall = _worst(v.decision for v in verdicts)
    # The envelope carries the strongest thing that happened to the call, and the
    # applied items by name. A reader of ``decision`` alone must not be able to
    # conclude the whole call succeeded — §?'s lesson, applied to a set.
    worst = next((v for v in verdicts if v.decision is overall
                  and v.decision is not Decision.ALLOW), None)
    return EvalResult(
        decision=overall,
        rule=(worst.rule if worst is not None
              else (merged.rule if merged is not None else "allow")),
        gates=merged.gates if merged is not None else (),
        reason_code=worst.reason_code if worst is not None else "",
        retry_class=worst.retry_class if worst is not None else None,
        feedback=(merged.feedback if merged is not None
                  else decided[0].feedback),
        ticket=merged.ticket if merged is not None else None,
        output=merged.output if merged is not None else None,
        scope_applied=merged.scope_applied if merged is not None else (),
        items=tuple(verdicts),
        applied=tuple(applied),
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
    obligations: Mapping[str, ObligationRegistry] | None = None,
    dedupe_window_s: float | None = None,
    agent: str = "unknown",
    enforcement: EnforcementMode = EnforcementMode.ENFORCED,
    scope_measure_cap: int = SCOPE_MEASURE_CAP,
) -> BatchResult:
    """Evaluate a SIF batch atomically (spec §12, §?; SIF §5).

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

    if enforcement is EnforcementMode.ADVISORY:
        # A batch is decided atomically, so its advice is a property of the
        # batch: enforcement would have refused ALL of it at ``failing``, and no
        # single operation's record can say that. Every operation carries the
        # batch advice; each also carries its own.
        kill_halt = next(
            (
                d
                for d in decided
                if d.decision is Decision.HALT and _kill_caused(d.rule)
            ),
            None,
        )
        if kill_halt is not None:
            # The operator's cord is not translated, and a batch is all or
            # nothing: the kill refuses the whole batch, as it would enforcing.
            # The refusal is still an advisory deployment's refusal — its
            # records carry the mode stamp, or the report's dataset would show
            # phantom "enforced" traffic from a deployment that has none.
            failing = decided.index(kill_halt)
            audit = _AdvisoryAudit(audit)
        else:
            advisory = _AdvisoryAudit(audit)
            if failing is not None:
                advisory.batch_advice = {
                    "wouldRefuse": True,
                    "failingIndex": failing,
                    "decision": decided[failing].decision.value,
                }
            advices: list[Advice | None] = []
            coverages: list[Coverage] = []
            for i, d in enumerate(decided):
                decided[i], advice, coverage = _as_advised(d, scope_measure_cap)
                advices.append(advice)
                coverages.append(coverage)
            # ``advisory.advice``/``.coverage`` are set immediately before each
            # operation's commit, so every record gets its own operation's.
            return _commit_batch_advisory(
                decided, advices, coverages, advisory, actor, session,
                connectors=connectors, outbox=outbox,
                freshness=freshness, env_of=env_of, agent_name=agent_name,
                failure_mode=failure_mode, obligations=obligations,
                dedupe_window_s=dedupe_window_s,
            )

    if failing is not None:
        # Phase 2a — refuse the whole batch: no record/transition
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
            results=tuple(
                _stamp_feedback(r, d) for r, d in zip(results, decided, strict=True)
            ),
        )

    # Reservation pre-pass (v0.3; §? composition): every operation
    # that matched a consumable obligation reserves it BEFORE anything commits;
    # a refused reservation refuses the whole batch and releases every
    # reservation taken for it — no partial claim survives a refused batch.
    reserved: list[tuple[int, ObligationClaim]] = []
    reserve_failure: tuple[int, str] | None = None
    for i, d in enumerate(decided):
        plan = claim_evidence(d.gate_results)
        if plan is None:
            continue
        claim, refusal = _reserve_claim(plan, obligations)
        if refusal is not None:
            reserve_failure = (i, refusal)
            break
        assert claim is not None
        reserved.append((i, claim))
        decided[i] = replace(d, claim=claim)
    if reserve_failure is not None:
        for _i, claim in reserved:
            _release_claim(claim, obligations)
        failing, rule = reserve_failure
        results = []
        for i, d in enumerate(decided):
            if i == failing:
                results.append(
                    _terminal(
                        Decision.DENY, rule, d.call, d.resolved, actor, session,
                        audit, agent_name, gate_results=d.gate_results,
                        scope_applied=d.scope_applied,
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
            decision=Decision.DENY,
            failing_index=failing,
            results=tuple(
                _stamp_feedback(r, d) for r, d in zip(results, decided, strict=True)
            ),
        )

    # Phase 2b — commit: stage every hold/effect, execute every read/write, in
    # submission order. Per §4.4 the record ops commit atomically with the
    # staging — the in-memory reference approximates that shared transaction by
    # committing sequentially after the all-operations decision above; the
    # SQL-class connector binds them in one database transaction.
    # STONEFOLD-AMBIGUITY: spec §12/§? defines batch atomicity for the
    # *decision*; a dependency failure mid-commit (outbox/connector down after
    # earlier operations committed) is governed per-operation by §10 and is not
    # rolled back here.
    results = [
        _stamp_feedback(
            _commit(
                d, actor, session, audit=audit, connectors=connectors, outbox=outbox,
                freshness=freshness, env=env_of(i), agent_name=agent_name,
                failure_mode=failure_mode, obligations=obligations,
                dedupe_window_s=dedupe_window_s, enforcement=enforcement,
            ),
            d,
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


def _commit_batch_advisory(
    decided: list[_Decided],
    advices: list[Advice | None],
    coverages: list[Coverage],
    advisory: _AdvisoryAudit,
    actor: Actor,
    session: Session,
    *,
    connectors: ConnectorRegistry | None,
    outbox: OutboxStore | None,
    freshness: FreshnessConfig | None,
    env_of: Any,
    agent_name: str,
    failure_mode: FailureMode,
    obligations: Mapping[str, ObligationRegistry] | None,
    dedupe_window_s: float | None,
) -> BatchResult:
    """Commit every operation of an advisory batch, in submission order.

    Nothing refuses the batch — that is the mode. The advice and coverage for
    each operation are set on the sink immediately before its commit, so each
    record carries the verdict its own operation earned while ``batchAdvice``
    carries the batch's.

    A DENY/HALT can still appear here: an unresolvable operation's deny stands
    (there is no connector to forward to), and the commit phase can fail for
    real (a dispatch failure, a lost scope). Both are reported exactly as the
    enforcing path reports them — the effects of the other operations have
    already happened, which is what the mode means.
    """
    results = []
    for i, d in enumerate(decided):
        advisory.advice = advices[i]
        advisory.coverage = coverages[i]
        results.append(
            _stamp_feedback(
                _commit(
                    d, actor, session, audit=advisory, connectors=connectors,
                    outbox=outbox, freshness=freshness, env=env_of(i),
                    agent_name=agent_name, failure_mode=failure_mode,
                    obligations=obligations, dedupe_window_s=dedupe_window_s,
                    enforcement=EnforcementMode.ADVISORY,
                ),
                d,
            )
        )
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
    """Steps 1–5 for one operation (spec §12) — the verdict, nothing committed."""

    # 1. RESOLVE (spec §12 step 1) — done *first* so every terminal record, even a
    # halt or a refusal, carries the resolved kind/resource/action the audit
    # requires (spec §11). An unknown name short-circuits to DENY before any policy
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

    # the agent-feedback level this action's policy declares — stamped
    # on every decision from here down so the transport can redact the return.
    fb = policy.feedback_for(resolved)

    # 2. AUTHORIZE — spec §6.2: deny-wins → default-deny → allow.
    authz = policy.authorize(resolved)
    if not authz.allowed:
        return _Decided(call, resolved, Decision.DENY, authz.rule, feedback=fb)

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
                            scope_applied=scope_applied, feedback=fb,
                        )
                elif probe.value is None:
                    return _Decided(
                        call, resolved, Decision.DENY, "scope-denied",
                        scope_applied=scope_applied, feedback=fb,
                    )

    # 4. GATES — evaluate the matching gates (spec §7/§12 step 4). Any FAIL ⇒
    # DENY (short-circuited before approvals); else any HOLD ⇒ HOLD (staged at
    # commit).
    gate_trace: tuple[GateResult, ...] = ()
    if gates is not None:
        outcome = gates.evaluate(resolved, actor, session, policy, env or RequestEnv())
        if outcome.outcome is Outcome.FAIL:
            return _Decided(
                call, resolved, Decision.DENY, outcome.reason or "gate-fail",
                gate_results=outcome.results, scope_pred=scope_pred,
                scope_applied=scope_applied, feedback=fb,
            )
        if outcome.outcome is Outcome.HOLD:
            return _Decided(
                call, resolved, Decision.HOLD, outcome.reason or "gate-hold",
                gate_results=outcome.results, approval=outcome.approval,
                releases=outcome.releases,
                scope_pred=scope_pred, scope_applied=scope_applied, feedback=fb,
            )
        gate_trace = outcome.results

    # 5. KILL — the chokepoint check (spec §12 step 5, design §8.3 point 2). An
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
                    scope_pred=scope_pred, scope_applied=scope_applied, feedback=fb,
                )
            order = None
        else:
            order = kill_probe.value
        if order is not None:
            return _Decided(
                call, resolved, Decision.HALT, f"kill:{order.id}",
                outcome="halted", gate_results=gate_trace,
                scope_pred=scope_pred, scope_applied=scope_applied, feedback=fb,
            )

    return _Decided(
        call, resolved, Decision.ALLOW, authz.rule, gate_results=gate_trace,
        scope_pred=scope_pred, scope_applied=scope_applied, feedback=fb,
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
    obligations: Mapping[str, ObligationRegistry] | None = None,
    dedupe_window_s: float | None = None,
    enforcement: EnforcementMode = EnforcementMode.ENFORCED,
) -> EvalResult:
    """Step 6/7 for one decided operation: stage/execute, then audit (spec §12).

    ``enforcement`` is a LABEL, never a branch: it is stamped on the row this
    step stages so the records that row writes later — settle, dispatch failure,
    cancellation — say which deployment produced them. Nothing in this function
    may read it to decide anything, or the two modes stop computing the same
    thing and the whole counterfactual claim goes with it.
    """

    call, resolved = decided.call, decided.resolved

    # A steps-1–5 refusal is terminal as-is.
    if decided.decision in (Decision.DENY, Decision.HALT):
        return _terminal(
            decided.decision, decided.rule, call, resolved, actor, session, audit,
            agent_name, gate_results=decided.gate_results,
            scope_applied=decided.scope_applied, outcome=decided.outcome,
        )

    assert resolved is not None  # ALLOW/HOLD always carries the resolved action

    # v0.3: the decision matched a consumable obligation — the row to
    # be staged must hold its reservation BEFORE the staging commit returns
    # (this closes the decide→dispatch double-spend window the TTL alone does
    # not). A batch pre-pass may have reserved already (``decided.claim``).
    claim_plan = claim_evidence(decided.gate_results)

    if decided.decision is Decision.HOLD:
        # A HOLD suspends the action: stage it as PENDING_APPROVAL so a human
        # can release it later (design §7). The ticket is returned to the agent.
        ticket = None
        if outbox is not None:
            ob = outbox
            # v0.3: holds that are the same question collapse — the
            # same (agent, action, reason code, candidate refs) as an OPEN held
            # row within the deployment's dedupe window bumps that row's
            # attempt count instead of queueing a second item. The agent gets
            # the SAME ticket; the attempt is audited as always. Denies are
            # cheap; holds spend human attention.
            duplicate = _find_duplicate_hold(
                ob, decided, agent_name, env, dedupe_window_s
            )
            if duplicate is not None:
                bumped = guard(
                    lambda: ob.bump_attempts(duplicate.id),
                    reason="outbox-unavailable",
                )
                if not isinstance(bumped, Unavailable):
                    return _terminal(
                        Decision.HOLD, decided.rule, call, resolved, actor,
                        session, audit, agent_name,
                        gate_results=decided.gate_results, ticket=duplicate.id,
                        scope_applied=decided.scope_applied,
                        outcome="hold-deduped",
                        approval=_approval_audit(
                            decided.approval, duplicate.id, decided.releases
                        ),
                    )
                # the bump raced the row's settlement — fall through and stage.
            expiry = _staging_expiry(freshness, env, resolved)
            if isinstance(expiry, Unavailable):
                return _terminal(
                    Decision.DENY, "freshness-unavailable", call, resolved, actor,
                    session, audit, agent_name, gate_results=decided.gate_results,
                    scope_applied=decided.scope_applied,
                )
            expires_at: datetime | None = expiry
            claim = decided.claim
            if claim is None and claim_plan is not None:
                claim, refusal = _reserve_claim(claim_plan, obligations)
                if refusal is not None:
                    # AlreadyReserved/AlreadyConsumed between decision and
                    # staging, or no adapter: refuse — never stage unreserved.
                    return _terminal(
                        Decision.DENY, refusal, call, resolved, actor, session,
                        audit, agent_name, gate_results=decided.gate_results,
                        scope_applied=decided.scope_applied,
                    )
            held = guard(
                lambda: ob.stage(
                    resolved=resolved, actor=actor, session_id=session.id,
                    agent=agent_name, state=PendingState.PENDING_APPROVAL,
                    correlation_id=session.correlation_id,
                    gates=decided.gate_results, approval=decided.approval,
                    releases=decided.releases,
                    expires_at=expires_at,
                    staged_at=env.now if env is not None else None,
                    obligation=claim,
                    enforcement=enforcement,
                ),
                reason="outbox-unavailable",
            )
            if isinstance(held, Unavailable):
                # can't durably suspend the action ⇒ fail closed (design §11/§12);
                # the reservation is returned (idempotent — a crash instead of an
                # exception leaves an orphan the adapter's TTL expires, R6).
                _release_claim(claim, obligations)
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
                consumption=claim.audit_dict("reserved") if claim is not None else None,
            )
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
            effect_claim = decided.claim
            if effect_claim is None and claim_plan is not None:
                effect_claim, refusal = _reserve_claim(claim_plan, obligations)
                if refusal is not None:
                    return _terminal(
                        Decision.DENY, refusal, call, resolved, actor, session,
                        audit, agent_name, gate_results=gate_trace,
                        scope_applied=decided.scope_applied,
                    )
            staged = guard(
                lambda: ob.stage(
                    resolved=resolved, actor=actor, session_id=session.id,
                    agent=agent_name, state=PendingState.PENDING,
                    correlation_id=session.correlation_id,
                    gates=gate_trace, compensation=resolved.compensation,
                    expires_at=effect_expires_at,
                    staged_at=env.now if env is not None else None,
                    obligation=effect_claim,
                    enforcement=enforcement,
                ),
                reason="outbox-unavailable",
            )
            if isinstance(staged, Unavailable):
                # the durable staging+evidence substrate is down. We can neither
                # stage, approve, nor record the effect, so failureMode 'open' does
                # not apply here — always fail closed (design §11/§12, invariant 7).
                _release_claim(effect_claim, obligations)
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
                consumption=(
                    effect_claim.audit_dict("reserved")
                    if effect_claim is not None
                    else None
                ),
            )
        return _terminal(
            Decision.ALLOW, decided.rule, call, resolved, actor, session, audit,
            agent_name, gate_results=gate_trace, scope_applied=decided.scope_applied,
        )

    # observe/record/transition run through the connector now, scope applied
    # below the model (design §5).
    if connectors is not None:
        # a pinned connector whose loaded artifact no longer matches its
        # digest is a dependency failure (spec §10) — honour failureMode, with the
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
        # §?, inline form: a record/transition with a consumable match runs
        # reserve → execute → consume in one commit (there is no staged row to
        # carry the claim to a later settle). Execution failure releases.
        inline_claim = decided.claim
        if inline_claim is None and claim_plan is not None:
            inline_claim, refusal = _reserve_claim(claim_plan, obligations)
            if refusal is not None:
                return _terminal(
                    Decision.DENY, refusal, call, resolved, actor, session,
                    audit, agent_name, gate_results=gate_trace,
                    scope_applied=decided.scope_applied,
                )
        executed = guard(
            lambda: connectors.get(resolved.connector).execute(
                resolved, decided.scope_pred, actor
            ),
            reason="connector-unavailable",
        )
        if isinstance(executed, Unavailable):
            # connector/dependency failure ⇒ honour failureMode (spec §10). Closed
            # denies; open allows the read through with no output (low-stakes).
            # Either way the effect did not land — the reservation is returned.
            _release_claim(inline_claim, obligations)
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
            consumption=_consume_claim(inline_claim, obligations),
            # D-A4: the read ran unscoped, so the record carries what the
            # predicate would have taken away. The single most uncomfortable
            # number in the report, and the most persuasive one.
            scope_would_remove=(
                _measure_scope(decided.scope_measure, output, actor)
                if decided.scope_measure is not None
                else None
            ),
        )

    return _terminal(
        Decision.ALLOW, decided.rule, call, resolved, actor, session, audit,
        agent_name, gate_results=gate_trace, scope_applied=decided.scope_applied,
    )


def _find_duplicate_hold(
    outbox: OutboxStore,
    decided: _Decided,
    agent_name: str,
    env: RequestEnv | None,
    dedupe_window_s: float | None,
) -> PendingAction | None:
    """The open held row this hold would duplicate, or ``None``.

    Requires the deployment to have configured a dedupe window AND an injected
    clock (the window is meaningless without one), and only code-bearing holds
    have a dedupe identity (``hold_dedupe_key``). A store error disables
    dedupe for this request — a failed collapse degrades to one extra queue
    item, never to a lost hold.
    """
    if dedupe_window_s is None or env is None or env.now is None:
        return None
    assert decided.resolved is not None
    key = hold_dedupe_key(agent_name, decided.resolved, decided.gate_results)
    if key is None:
        return None
    try:
        rows = outbox.list_by_state(PendingState.PENDING_APPROVAL)
    except Exception:
        return None
    for row in rows:
        if (env.now - row.created_at).total_seconds() > dedupe_window_s:
            continue
        if hold_dedupe_key(row.agent, row.resolved, row.gates) == key:
            return row
    return None


def _reserve_claim(
    plan: Mapping[str, Any],
    obligations: Mapping[str, ObligationRegistry] | None,
) -> tuple[ObligationClaim | None, str | None]:
    """Reserve the matched obligation for a row about to stage/execute: returns ``(claim, None)`` on success or ``(None, refusal-rule)``.

    ``AlreadyReserved``/``AlreadyConsumed`` refuse ``no-match`` — the line was
    spoken for between decision and commit. A missing/erroring adapter refuses
    ``reservation-unavailable`` unconditionally: staging a consumable match
    without its reservation would reopen the double-spend window, so
    ``failureMode: open`` does not apply here (same footing as the outbox
    itself being down). The reservation id is generated here — the commit
    phase is the I/O layer; determinism (invariant 1) protects ``_decide``.
    """
    registry_name = str(plan["registry"])
    ref = str(plan["refs"][0])
    adapter = obligations.get(registry_name) if obligations is not None else None
    if adapter is None:
        return None, "reservation-unavailable"
    intent_id = f"itn_{uuid.uuid4().hex}"
    try:
        outcome = adapter.reserve(ref, intent_id)
    except Exception:
        return None, "reservation-unavailable"
    if outcome is not ReserveOutcome.RESERVED:
        return None, "no-match"
    return (
        ObligationClaim(
            registry=registry_name,
            ref=ref,
            consume=str(plan["consume"]),
            capability=Capability(str(plan["capability"])),
            intent_id=intent_id,
        ),
        None,
    )


def _release_claim(
    claim: ObligationClaim | None,
    obligations: Mapping[str, ObligationRegistry] | None,
) -> None:
    """Return a reservation after a failed commit — best-effort and
    idempotent: if this call is lost (crash, adapter blip) the reservation is
    an orphan the adapter's own TTL expires (R6)."""
    if claim is None or obligations is None:
        return
    adapter = obligations.get(claim.registry)
    if adapter is None:
        return
    try:
        adapter.release(claim.ref, claim.intent_id)
    except Exception:
        pass  # orphan: expired by the adapter's reservation TTL (R6)


def _consume_claim(
    claim: ObligationClaim | None,
    obligations: Mapping[str, ObligationRegistry] | None,
) -> dict[str, Any] | None:
    """Consume an inline-executed action's reservation right after the
    connector confirmed (inline form) and return the §?
    ``consumption`` audit field. The effect has landed either way — a consume
    refusal/outage is recorded honestly, never hidden and never a rollback."""
    if claim is None:
        return None
    adapter = obligations.get(claim.registry) if obligations is not None else None
    if adapter is None:
        return claim.audit_dict("consume-unavailable")
    try:
        result = adapter.consume(claim.ref, claim.intent_id)
    except Exception:
        return claim.audit_dict("consume-unavailable")
    if result.outcome is ConsumeOutcome.CONSUMED:
        return claim.audit_dict("consumed", receipt=result.receipt)
    return claim.audit_dict("consume-refused")


def _stamp_feedback(result: EvalResult, decided: _Decided) -> EvalResult:
    """Stamp the policy-declared feedback level on the result AFTER the
    audit was written from it — the transport redacts by this stamp; the audit
    never sees a redacted record."""
    if result.feedback is decided.feedback:
        return result
    return result.model_copy(update={"feedback": decided.feedback})


def _staging_expiry(
    freshness: FreshnessConfig | None,
    env: RequestEnv | None,
    resolved: ResolvedAction,
) -> datetime | None | Unavailable:
    """The ``expires_at`` to stamp on a row being staged (v0.2).

    ``None`` when freshness is not configured (opt-in: pre-v0.2 behaviour).
    Freshness configured but no injected clock ⇒ ``Unavailable``: the gateway
    cannot bound the decision's validity, and §? requires every staged row's
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
    """Render the release contract(s) for the audit record (spec §11 ``approval``).

    A held action records *who* may release it and the quorum/timeout terms, with
    a ``pending`` status — the eventual approver(s)/resolver(s) and outcome are
    written when the row settles. The legacy top-level keys mirror the first
    contract (pre-v0.3 consumers); ``releases`` lists EVERY holding gate's
    contract, each with its cause, reason code, and evidence (I7).
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
    consumption: dict[str, Any] | None = None,
    scope_would_remove: dict[str, Any] | None = None,
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
            consumption=consumption,
            scope_would_remove=scope_would_remove,
        )
    )
    return result
