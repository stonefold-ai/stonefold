"""The gate engine (RFC §7/§12 step 4, design §6).

Evaluates every gate that matches an action, combined with **AND**, in
cheapest-deterministic-first order with ``requireApproval``/``dualAuthorization``
last. The first ``FAIL`` short-circuits to DENY — so a cheap failure is found
*before* an approval HOLD is ever raised (DoD ordering requirement). With no
failures, any HOLD makes the verdict HOLD; otherwise PASS.

The engine also injects the built-in transition guard: any ``transition`` action
re-checks its declared ``from`` states (RFC §7.6) even when the policy lists no
``precondition`` gate.

This module is the concrete ``GateEngine`` the pure pipeline depends on through
the ``stonefold_core.gating`` seam — it imports ``stonefold_core``/``stonefold_store`` but nothing
imports it back into the kernel.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any

from stonefold_core.condition import ConditionRuntimeError, EvalContext, make_window
from stonefold_core.enums import Kind, Outcome
from stonefold_core.freshness import VOLATILE_GATES, DispatchRevalidator
from stonefold_core.gating import ApprovalSpec, GateOutcome, RequestEnv
from stonefold_core.models import Actor, GateResult, ResolvedAction, Session
from stonefold_core.outbox import PendingAction
from stonefold_store import CounterStore, InMemoryCounterStore
from stonefold_gates import gates as g
from stonefold_gates.base import GateContext, GateFn, PreconditionCheck, passed
from stonefold_gates.content import ContentHookRegistry, default_hooks

if TYPE_CHECKING:
    from stonefold_core.compiler import CompiledPolicy

# gate name -> (cost, fn). Lower cost runs first; approvals are the most
# expensive so a cheaper FAIL always short-circuits before a HOLD is raised.
_GATES: dict[str, tuple[int, GateFn]] = {
    "valueLimit": (10, g.value_limit),
    "requireExplanation": (10, g.require_explanation),
    "window": (10, g.window_gate),
    "allowlist": (15, g.allowlist),
    "denylist": (15, g.denylist),
    "precondition": (20, g.precondition),
    "emissionControl": (20, g.emission_control),
    "disclosure": (20, g.disclosure),
    "rate": (30, g.rate),
    "quota": (30, g.quota),
    "quantityCap": (30, g.quantity_cap),
    "spendLimit": (30, g.spend_limit),
    "contentCheck": (50, g.content_check),
    "requireApproval": (90, g.require_approval),
    "dualAuthorization": (90, g.dual_authorization),
}

_TRANSITION_GUARD = "_transition_from"


def build_eval_context(
    resolved: ResolvedAction, actor: Actor, env: RequestEnv
) -> EvalContext:
    """Assemble the condition context from the request (design §10). Identity and
    ambient state come from ``actor``/``env`` — never the agent's ``data``."""
    action_ns: dict[str, Any] = {
        "kind": resolved.kind.value,
        "name": resolved.action,
        "resource": resolved.resource,
        "reversibility": resolved.attrs.reversibility.value,
        "emission": resolved.attrs.emission.value,
        "operativeForce": resolved.attrs.operativeForce.value,
        "resultSensitivity": resolved.attrs.resultSensitivity,
        "explainability": resolved.attrs.explainability.value,
    }
    actor_ns: dict[str, Any] = {"id": actor.id, "roles": sorted(actor.roles)}
    actor_ns.update(actor.claims)
    context_ns: dict[str, Any] = dict(env.context)
    if env.now is not None and "now" not in context_ns:
        context_ns["now"] = env.now
    functions: dict[str, Any] = {"now": lambda: env.now, "window": make_window}
    return EvalContext(
        namespaces={
            "action": action_ns,
            "data": dict(resolved.data),
            "resource": dict(env.resource),
            "actor": actor_ns,
            "context": context_ns,
        },
        functions=functions,
    )


class DefaultGateEngine:
    """The injected gate engine. Satisfies ``stonefold_core.gating.GateEngine``."""

    def __init__(
        self,
        registry: Any,
        *,
        counters: CounterStore | None = None,
        hooks: ContentHookRegistry | None = None,
        preconditions: Mapping[str, PreconditionCheck] | None = None,
    ) -> None:
        self.registry = registry
        self.counters: CounterStore = counters or InMemoryCounterStore()
        self.hooks: ContentHookRegistry = hooks or default_hooks()
        self.preconditions: dict[str, PreconditionCheck] = dict(preconditions or {})

    def _context(
        self,
        resolved: ResolvedAction,
        actor: Actor,
        session: Session,
        policy: "CompiledPolicy",
        env: RequestEnv,
    ) -> GateContext:
        return GateContext(
            resolved=resolved,
            actor=actor,
            session=session,
            env=env,
            eval_ctx=build_eval_context(resolved, actor, env),
            registry=self.registry,
            counters=self.counters,
            hooks=self.hooks,
            preconditions=self.preconditions,
            failure_mode=policy.policy.defaults.failureMode,
            agent=policy.agent,
        )

    def evaluate(
        self,
        resolved: ResolvedAction,
        actor: Actor,
        session: Session,
        policy: "CompiledPolicy",
        env: RequestEnv,
    ) -> GateOutcome:
        gctx = self._context(resolved, actor, session, policy, env)

        items: list[tuple[int, str, Any]] = []
        # built-in transition guard, always (RFC §7.6) — cheapest, runs first.
        if resolved.kind is Kind.TRANSITION and resolved.from_states:
            items.append((0, _TRANSITION_GUARD, {"from": list(resolved.from_states)}))
        for name, cfg in policy.gates_for(resolved).items():
            spec = _GATES.get(name)
            if spec is None:
                continue  # unknown gate name — the linter rejects these at load
            items.append((spec[0], name, cfg))
        items.sort(key=lambda t: t[0])

        results: list[GateResult] = []
        first_hold: GateResult | None = None
        first_hold_cfg: Any = None
        for _cost, name, cfg in items:
            result = self._run_one(name, cfg, gctx)
            results.append(result)
            if result.outcome is Outcome.FAIL:
                # short-circuit: DENY before any later/approval gate runs.
                return GateOutcome(Outcome.FAIL, tuple(results), reason=f"gate:{name}")
            if result.outcome is Outcome.HOLD and first_hold is None:
                first_hold = result
                first_hold_cfg = cfg
        if first_hold is not None:
            return GateOutcome(
                Outcome.HOLD,
                tuple(results),
                reason=f"gate:{first_hold.gate}",
                approval=_approval_spec(first_hold.gate, first_hold_cfg),
            )
        return GateOutcome(Outcome.PASS, tuple(results))

    def revalidate_volatile(
        self,
        resolved: ResolvedAction,
        actor: Actor,
        session: Session,
        policy: "CompiledPolicy",
        env: RequestEnv,
    ) -> GateResult | None:
        """Dispatch-time re-validation of the VOLATILE gates only (v0.4 CS-017).

        Re-runs ``allowlist``/``denylist``/``window``/``precondition``/
        ``emissionControl`` for a claimed staged row against dispatch-time state;
        the non-volatile gates (counters, approvals, content, value limits) stay
        decided — re-running them would double-count or re-request the grant.
        Returns the first non-PASS result (a HOLD here is treated as stale too:
        a claimed row cannot be re-suspended) or ``None`` when still fresh.
        """
        gctx = self._context(resolved, actor, session, policy, env)
        for name, cfg in policy.gates_for(resolved).items():
            if name not in VOLATILE_GATES:
                continue
            result = self._run_one(name, cfg, gctx)
            if result.outcome is not Outcome.PASS:
                return result
        return None

    def _run_one(self, name: str, cfg: Any, gctx: GateContext) -> GateResult:
        # `when:` makes ANY gate conditional (RFC §7). A runtime resolution error
        # in the condition is fail-closed for the gate (design §10; C8), distinct
        # from the condition evaluating to false (which deactivates the gate).
        if isinstance(cfg, Mapping) and "when" in cfg:
            try:
                active = _eval_when(cfg["when"], gctx)
            except ConditionRuntimeError as exc:
                return GateResult(
                    gate=name, outcome=Outcome.FAIL,
                    reason=f"fail-closed: condition error: {exc}",
                )
            if not active:
                return passed(name, "inactive: when=false")
        if name == _TRANSITION_GUARD:
            return g.check_from_states(cfg["from"], gctx)
        return _GATES[name][1](cfg, gctx)


def make_dispatch_revalidator(
    engine: DefaultGateEngine, policy: "CompiledPolicy"
) -> DispatchRevalidator:
    """Bind an engine + compiled policy into the ``DispatchWorker``'s CS-017
    re-validation hook. The row's actor/session are the STAGED identity
    (invariant 3 — never re-derived); only the clock is dispatch-time."""

    def revalidate(row: PendingAction, now: datetime) -> GateResult | None:
        session = Session(id=row.session_id, correlation_id=row.correlation_id)
        return engine.revalidate_volatile(
            row.resolved, row.actor, session, policy, RequestEnv(now=now)
        )

    return revalidate


def _eval_when(src: str, gctx: GateContext) -> bool:
    from stonefold_core.condition import evaluate_str

    return evaluate_str(src, gctx.eval_ctx)


def _approvers(cfg: Any) -> tuple[str, ...]:
    if not isinstance(cfg, Mapping):
        return ()
    raw = cfg.get("approvers")
    if raw is None:
        return ()
    return tuple(raw) if isinstance(raw, (list, tuple)) else (str(raw),)


def _approval_spec(gate_name: str, cfg: Any) -> ApprovalSpec:
    """Translate the holding gate's config into the outbox's approval contract."""
    on_timeout = cfg.get("onTimeout", "deny") if isinstance(cfg, Mapping) else "deny"
    if gate_name == "dualAuthorization":
        return ApprovalSpec(
            quorum=2, dual_auth=True, distinct_from_actor=True,
            approvers=_approvers(cfg), on_timeout=on_timeout,
        )
    quorum = int(cfg.get("quorum", 1)) if isinstance(cfg, Mapping) else 1
    return ApprovalSpec(
        quorum=quorum, approvers=_approvers(cfg), on_timeout=on_timeout,
    )
