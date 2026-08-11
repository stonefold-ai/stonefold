# SPDX-License-Identifier: Apache-2.0
"""Transports and the single chokepoint (design §0, §1; spec §3).

The most important implementation fact (design §0) is that **the gateway is the
only path from the agent to any connector**. ``Gateway`` makes that concrete: it
holds the injected enforcement dependencies and exposes ``submit`` *once*, so
every transport routes through the same ``enforce`` call and none can diverge.

Two transports sit in front of it (spec §3):

* **The declared surface** (design §1.1): the agent's tools are generated from the
  registry — one typed tool per declared action (``mcp_tool_schemas``), served with
  retrieval over them (``stonefold_gateway.mcp_search``) for a registry too large to
  put in front of a model at once. Coverage is structural: a tool exists because an
  action is declared. A client that would rather build the intent itself posts it to
  ``submit_intent`` (``SifNativeTransport``) and lands in the same place.
* **Interception / MCP proxy** (design §1.2): the agent keeps its tools, but each
  call is mapped to a declared action and enforced. The mapping is the coverage
  boundary, so: an **unmapped tool denies** (never pass-through, review note),
  and a **free-form-string pass-through requires explicit acknowledgement**
  (review note). ``interception_coverage_check`` fails startup if any configured
  tool endpoint bypasses the gateway.

The advisory profile inverts the coverage boundary and nothing else. An advisory
deployment refuses nothing but a kill order, so an unmapped tool cannot be denied
— it is forwarded to the endpoint it was going to anyway and recorded
``coverage: unjudged``, which is the honest half of the measurement: *here is
everything your agents did, and here is the part a policy could judge*. That path
exists only where the deployment supplied the upstream (``ToolForwarder``) and
only under advisory; on an enforcing gateway configuring one fails startup.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from stonefold_core import (
    Actor,
    AuditSink,
    BatchResult,
    CompiledPolicy,
    Connectors,
    Decision,
    EnforcementMode,
    EvalResult,
    GateEngine,
    InMemoryRegistry,
    KillStore,
    OutboxStore,
    RawCall,
    RequestEnv,
    Session,
    build_record,
    enforce,
    enforce_batch,
)
from stonefold_core.enums import Coverage
from stonefold_core.feedback import agent_view, agent_view_batch
from stonefold_core.freshness import FreshnessConfig
from stonefold_core.models import Advice
from stonefold_core.obligation import ObligationRegistry
from stonefold_core.pipeline import ADVISORY_RULE
from stonefold_core.scope import ScopeResolver

# The structural refusal for a tool the interception mapping does not cover, and
# the rule for a forward that could not reach the endpoint behind it.
UNMAPPED_TOOL = "unmapped-tool"
UPSTREAM_UNAVAILABLE = "upstream-unavailable"


class ToolForwarder(Protocol):
    """The real tool endpoint an interception deployment sits in front of.

    Supplied by the deployment, because only the deployment knows where its
    agents' tools actually live. Advisory forwarding needs it: the gateway can
    refuse a call it cannot map while knowing nothing about it, but it cannot let
    one through unless it knows where the call was going — which is why §5's
    coverage rule has two halves, and why an advisory deployment without a
    forwarder still refuses (and records) an unmapped tool.
    """

    def __call__(self, tool: str, args: Mapping[str, Any]) -> Any: ...


class Gateway:
    """The one chokepoint. Holds the enforcement dependencies and runs ``enforce``
    for every transport (design §0). Actor and session are supplied **per call**
    from the authenticated transport — never from the agent payload (invariant 3).
    """

    def __init__(
        self,
        *,
        registry: InMemoryRegistry,
        audit: AuditSink,
        policy: CompiledPolicy | None = None,
        gates: GateEngine | None = None,
        scopes: ScopeResolver | None = None,
        connectors: Connectors | None = None,
        outbox: OutboxStore | None = None,
        kill: KillStore | None = None,
        env: RequestEnv | None = None,
        env_factory: Callable[[RawCall], RequestEnv] | None = None,
        freshness: FreshnessConfig | None = None,
        obligations: Mapping[str, ObligationRegistry] | None = None,
        dedupe_window_s: float | None = None,
        agent: str = "unknown",
    ) -> None:
        self._registry = registry
        self._audit = audit
        self._policy = policy
        self._gates = gates
        self._scopes = scopes
        self._connectors = connectors
        self._outbox = outbox
        self._kill = kill
        self._env = env
        # A live gateway needs a fresh per-request ``RequestEnv`` — the wall clock
        # the time-based gates read (``rate``/``window``) and the resolved-resource
        # attributes a gate's ``per:``/``when:`` references (e.g. ``resource.payeeId``).
        # ``env_factory`` builds it from the call; a fixed ``env`` (deterministic
        # tests) is still honoured when no factory is supplied. The factory derives
        # context only from the gateway's own stores — never the agent's identity
        # (invariant 3 stays intact: identity is the per-call ``actor`` argument).
        self._env_factory = env_factory
        # Decision freshness (v0.2), opt-in: with a config every staged
        # effect gets a TTL stamped from the request env's clock.
        self._freshness = freshness
        # v0.3: the obligation-registry adapters the commit phase
        # reserves/consumes from — the same map the gate engine matches against.
        self._obligations = obligations
        # v0.3: the deployment's hold-dedupe window (seconds); ``None``
        # disables collapsing — deployment configuration, like decision TTLs.
        self._dedupe_window_s = dedupe_window_s
        self._agent = policy.agent if policy is not None else agent
        # advisory profile. The mode comes from the compiled policy,
        # which only compiles at all where the deployment permitted advisory
        # (``load_policy(advisory_permitted=...)``) — the two keys are checked
        # at load, so by the time a gateway exists the question is settled.
        self._enforcement = (
            policy.policy.effective_enforcement
            if policy is not None
            else EnforcementMode.ENFORCED
        )

    @property
    def enforcement(self) -> EnforcementMode:
        """What this gateway does with its verdicts. Read by the startup banner
        and the stats surface: an advisory deployment is never quiet about it."""
        return self._enforcement

    @property
    def registry(self) -> InMemoryRegistry:
        return self._registry

    @property
    def agent(self) -> str:
        return self._agent

    def submit(
        self,
        *,
        resource: str,
        action: str | None,
        data: Mapping[str, Any] | None,
        actor: Actor,
        session: Session,
    ) -> EvalResult:
        """The single enforcement entry point shared by both transports.

        The returned result is the AGENT's view: redacted to the
        policy-declared feedback level. The audit record was written from the
        full result inside ``enforce`` — redact on return, never on write.
        """
        raw = RawCall(resource=resource, action=action, data=dict(data or {}))
        env = self._env_factory(raw) if self._env_factory is not None else self._env
        result = enforce(
            raw,
            actor,
            session,
            registry=self._registry,
            audit=self._audit,
            policy=self._policy,
            gates=self._gates,
            env=env,
            scopes=self._scopes,
            connectors=self._connectors,
            outbox=self._outbox,
            kill=self._kill,
            freshness=self._freshness,
            obligations=self._obligations,
            dedupe_window_s=self._dedupe_window_s,
            agent=self._agent,
            enforcement=self._enforcement,
        )
        return agent_view(result)

    def submit_batch(
        self,
        operations: Sequence[Mapping[str, Any]],
        *,
        actor: Actor,
        session: Session,
    ) -> BatchResult:
        """Enforce a SIF batch atomically (spec §12, §?; SIF §5).

        Every operation is decided first; any DENY/HALT refuses the whole batch
        before anything commits or stages. Same identity rule as ``submit``:
        actor/session come from the authenticated transport, never the payload.
        """
        raws = [
            RawCall(
                resource=str(op.get("resource", "")),
                action=op.get("action"),
                data=dict(op.get("data") or {}),
            )
            for op in operations
        ]
        envs: Sequence[RequestEnv | None] | None
        if self._env_factory is not None:
            envs = [self._env_factory(raw) for raw in raws]
        elif self._env is not None:
            envs = [self._env] * len(raws)
        else:
            envs = None
        return agent_view_batch(
            enforce_batch(
                raws,
                actor,
                session,
                registry=self._registry,
                audit=self._audit,
                policy=self._policy,
                gates=self._gates,
                envs=envs,
                scopes=self._scopes,
                connectors=self._connectors,
                outbox=self._outbox,
                kill=self._kill,
                freshness=self._freshness,
                obligations=self._obligations,
                dedupe_window_s=self._dedupe_window_s,
                agent=self._agent,
                enforcement=self._enforcement,
            )
        )

    def refuse(
        self,
        *,
        reason: str,
        resource: str,
        action: str | None,
        data: Mapping[str, Any] | None,
        actor: Actor,
        session: Session,
    ) -> EvalResult:
        """An audited *structural* refusal that never enters the policy pipeline —
        used by the proxy for an unmapped tool (design §1.2: unmapped ⇒ deny).

        The record carries the deployment's mode, like every other record: a
        refusal written by an advisory gateway that said ``enforced`` would put
        traffic in the dataset from a deployment that has none. Coverage is
        ``unjudged`` in either mode — a structural refusal is the gateway saying
        it could not judge the call, not a verdict about it.
        """
        from stonefold_core.reasons import classify

        reason_code, retry_class = classify(Decision.DENY, reason, ())
        result = EvalResult(
            decision=Decision.DENY, rule=reason,
            reason_code=reason_code, retry_class=retry_class,
        )
        self._audit.write(
            build_record(
                agent=self._agent,
                actor=actor,
                session=session,
                call=RawCall(resource=resource, action=action, data=dict(data or {})),
                resolved=None,
                result=result,
                outcome="not_executed",
                enforcement=self._enforcement,
                coverage=Coverage.UNJUDGED,
            )
        )
        return result

    def forward_unjudged(
        self,
        *,
        tool: str,
        args: Mapping[str, Any],
        actor: Actor,
        session: Session,
        upstream: ToolForwarder,
    ) -> EvalResult:
        """Advisory only: let a call the gateway could not judge reach the
        endpoint it was going to, and record that it could not judge it.

        Default-deny inverts here. Under enforcement an unjudgeable call is
        refused and the architecture is done; an advisory deployment promised to
        refuse nothing, so refusing what it cannot map would be a second silent
        lever beside the kill switch. It forwards instead, and the report's first
        section becomes "here is everything your agents did, and here is the part
        a policy could judge" — a pilot reporting 94% coverage is telling the
        truth and starting the conversation about the other 6%.

        The record is never counted as an advisory allow: ``coverage: unjudged``
        says the gateway saw the action and could not decide it, and ``advised``
        keeps the refusal an enforcing deployment would have made — a
        default-deny reflex, which is exactly why the coverage field exists to
        say it was not a judgement.
        """
        if self._enforcement is not EnforcementMode.ADVISORY:
            # The only path where a call reaches its endpoint without a verdict.
            # The check lives here, in the class that owns the mode, and not only
            # in the caller that happens to know about it today.
            raise CoverageError(
                "forward_unjudged is advisory-only: an enforcing gateway refuses "
                "what it cannot map (invariant 2, and design §1.2's unmapped ⇒ deny)"
            )
        from stonefold_core.reasons import classify

        reason_code, retry_class = classify(Decision.DENY, UNMAPPED_TOOL, ())
        advised = Advice(
            decision=Decision.DENY, rule=UNMAPPED_TOOL,
            reason_code=reason_code, retry_class=retry_class,
        )
        call = RawCall(resource=tool, action=None, data=dict(args))
        try:
            output = upstream(tool, args)
        except Exception:
            # The forward failed, so the action did not happen — and that is the
            # estate's own outage, not a policy result (D-A3). It is recorded as
            # unjudged rather than as anything the gateway decided, and the
            # caller sees the failure it would have seen without us in the path.
            # The record's code is the failure's own; the unmapped-tool code
            # stays on ``advised``, which is the only thing it classified.
            failure_code, failure_retry = classify(
                Decision.DENY, UPSTREAM_UNAVAILABLE, ()
            )
            self._record_unjudged(
                call, actor, session, advised=advised, outcome="failure",
                result=EvalResult(
                    decision=Decision.DENY, rule=UPSTREAM_UNAVAILABLE,
                    reason_code=failure_code, retry_class=failure_retry,
                ),
            )
            raise
        # The actor sees an ordinary tool result. It must not learn that the
        # call went unjudged: an agent that knows which of its tools are
        # ungoverned is no longer producing the traffic being measured.
        result = EvalResult(decision=Decision.ALLOW, rule=ADVISORY_RULE, output=output)
        self._record_unjudged(
            call, actor, session, advised=advised, outcome="success",
            result=result, record_rule=UNMAPPED_TOOL,
        )
        return result

    def _record_unjudged(
        self,
        call: RawCall,
        actor: Actor,
        session: Session,
        *,
        result: EvalResult,
        advised: Advice,
        outcome: str,
        record_rule: str | None = None,
    ) -> None:
        """One record for an action the gateway forwarded without judging it."""
        self._audit.write(
            build_record(
                agent=self._agent,
                actor=actor,
                session=session,
                call=call,
                resolved=None,
                result=result,
                outcome=outcome,
                enforcement=EnforcementMode.ADVISORY,
                advised=advised,
                coverage=Coverage.UNJUDGED,
                record_rule=record_rule,
            )
        )


# --- Declared shape: shared by every transport ----------------------------
class InvalidIntentError(ValueError):
    """A submitted intent does not match the declared shape of its action.

    Carries a SIF §6 structured error — code, pointer, message — because the
    thing an agent most needs from a rejection is *which field to fix*. A bare
    reason code with no pointer reads as a policy refusal, and an agent that
    misreads a malformed call as a refusal stops trying instead of correcting.
    """

    def __init__(self, code: str, pointer: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.pointer = pointer
        self.message = message

    def as_error(self) -> dict[str, Any]:
        return {"code": self.code, "pointer": self.pointer, "message": self.message}


def validate_intent_data(
    registry: InMemoryRegistry, resource: str, action: str, data: Mapping[str, Any]
) -> None:
    """Check ``data`` against the action's declared shape, if it declares one.

    Deliberately shallow: required fields present, no unknown fields, declared
    enums honoured. It is not a JSON Schema engine — the point is to name the
    field the caller got wrong.

    An action that declares no ``data`` (``ActionDef.data is None``) is not
    checked at all, so a registry that has not declared any shapes behaves
    exactly as it did before.
    """
    rdef = registry.file.resources.get(resource)
    adef = rdef.actions.get(action) if rdef is not None else None
    if adef is None or adef.data is None:
        return
    for name, prop in sorted(adef.data.items()):
        if prop.required and data.get(name) in (None, ""):
            raise InvalidIntentError(
                "MISSING_FIELD", f"data.{name}",
                f"{resource}.{action} requires {name}"
                + (f" ({prop.label})" if prop.label else ""),
            )
        value = data.get(name)
        if value is not None and prop.values and str(value) not in prop.values:
            raise InvalidIntentError(
                "BAD_VALUE", f"data.{name}",
                f"{name} must be one of {', '.join(prop.values)}",
            )
    for name in sorted(data):
        if name not in adef.data:
            takes = ", ".join(sorted(adef.data)) or "no data at all"
            raise InvalidIntentError(
                "UNKNOWN_FIELD", f"data.{name}",
                f"{resource}.{action} has no field {name}; it takes {takes}",
            )


# --- The intent endpoint: one attempted action in, one decision out -------
class SifNativeTransport:
    """The executor behind ``POST /submit_intent``: an intent arrives in the SIF
    wire form and routes straight to the gateway. Every other transport ends up
    here, so the decision cannot depend on how the call was expressed."""

    def __init__(self, gateway: Gateway) -> None:
        self._gateway = gateway

    def submit_intent(
        self, payload: Mapping[str, Any], *, actor: Actor, session: Session
    ) -> EvalResult:
        # Shape first, policy second. A malformed intent is not a refusal and is
        # not reported as one.
        validate_intent_data(
            self._gateway.registry,
            str(payload.get("resource", "")),
            str(payload.get("action") or ""),
            payload.get("data") or {},
        )
        return self._gateway.submit(
            resource=str(payload.get("resource", "")),
            action=payload.get("action"),
            data=payload.get("data") or {},
            actor=actor,
            session=session,
        )

    def submit_intent_batch(
        self, operations: Sequence[Mapping[str, Any]], *, actor: Actor, session: Session
    ) -> BatchResult:
        """The SIF wire form ``{"operations": [...]}`` (SIF §5) — decided
        atomically per spec §12 / §?."""
        for index, op in enumerate(operations):
            try:
                validate_intent_data(
                    self._gateway.registry,
                    str(op.get("resource", "")),
                    str(op.get("action") or ""),
                    op.get("data") or {},
                )
            except InvalidIntentError as exc:
                # SIF §6 already points at the failing operation in a refused
                # batch; a malformed one points at the field inside it.
                raise InvalidIntentError(
                    exc.code, f"operations[{index}].{exc.pointer}", exc.message
                ) from None
        return self._gateway.submit_batch(operations, actor=actor, session=session)


# --- Interception / MCP proxy (design §1.2) -------------------------------
def mcp_tool_schemas(registry: InMemoryRegistry) -> list[dict[str, Any]]:
    """One typed tool per declared action, generated from the registry.

    This is the MCP-facing view of the same catalogue every other surface uses:
    the tool name is ``resource.action`` (1:1 with a declared action), the
    description is the action's ``label``, and the input schema is the action's
    declared ``data`` shape — or a bare object where the action declares none,
    in which case the agent is being asked to guess and the registry should
    declare the shape.

    Nothing here widens what an agent can reach: a tool exists because an action
    is declared, and ``MCPProxy`` denies any call it cannot map to one.
    """
    tools: list[dict[str, Any]] = []
    for resource, rdef in sorted(registry.file.resources.items()):
        for action, adef in sorted(rdef.actions.items()):
            schema = adef.data_schema() or {"type": "object", "properties": {}}
            description = (
                adef.label
                or f"{resource}.{action} — a declared {adef.kind.value} action."
            )
            tools.append({
                "name": f"{resource}.{action}",
                "description": description,
                "input_schema": schema,
            })
    return tools


class CoverageError(RuntimeError):
    """Startup coverage failure (design §1 review notes): an agent has a tool path
    that does not pass through the gateway, or an unacknowledged free-form
    pass-through tool. The gateway MUST refuse to start (invariant: no tool is
    reachable except through the gateway)."""


@dataclass(frozen=True)
class ToolMapping:
    """Maps an intercepted tool call to a declared action (design §1.2).

    ``arg_map`` renames tool argument keys to the action's ``data`` keys (identity
    when empty). ``free_form`` flags a tool whose arguments are an opaque string
    (e.g. a raw ``run_sql``) — a high-risk pass-through that must be explicitly
    acknowledged before the proxy will start (review note).
    """

    tool: str
    resource: str
    action: str
    arg_map: Mapping[str, str] = field(default_factory=dict)
    free_form: bool = False

    def to_data(self, args: Mapping[str, Any]) -> dict[str, Any]:
        if not self.arg_map:
            return dict(args)
        return {self.arg_map.get(k, k): v for k, v in args.items()}


class MCPProxy:
    """A tool/MCP reverse proxy in front of the gateway (design §1.2).

    ``upstream`` is the real tool endpoint this proxy sits in front of, supplied
    only by an **advisory** deployment. With it, a tool the mapping does not
    cover is forwarded and recorded ``coverage: unjudged`` instead of refused;
    without it, an unmapped tool is refused in either mode and recorded the same
    way — the gateway cannot let through what it cannot address.
    """

    def __init__(
        self,
        gateway: Gateway,
        mappings: Iterable[ToolMapping],
        *,
        acknowledge_freeform: bool = False,
        upstream: ToolForwarder | None = None,
    ) -> None:
        self._gateway = gateway
        self._by_tool: dict[str, ToolMapping] = {m.tool: m for m in mappings}
        self._upstream = upstream
        unacknowledged = sorted(
            m.tool for m in self._by_tool.values() if m.free_form and not acknowledge_freeform
        )
        if unacknowledged:
            raise CoverageError(
                "free-form pass-through tools require explicit acknowledgement: "
                f"{unacknowledged}"
            )
        if upstream is not None and gateway.enforcement is not EnforcementMode.ADVISORY:
            # Startup, not request time. A forwarder an enforcing gateway never
            # calls is still a bypass sitting in the configuration, waiting for
            # someone to flip the policy back — and the conversion out of a pilot
            # is exactly when nobody is looking at the proxy's arguments.
            raise CoverageError(
                "an upstream forwarder is advisory-only: on an enforcing gateway "
                "it is a bypass waiting for a mode flip. Remove it when the "
                "advisory pilot converts to enforcement."
            )

    def call_tool(
        self, tool: str, args: Mapping[str, Any], *, actor: Actor, session: Session
    ) -> EvalResult:
        mapping = self._by_tool.get(tool)
        if mapping is None:
            if self._upstream is not None:
                # advisory only, enforced by the constructor above and asserted
                # again by the gateway: forward what we cannot judge, and say so.
                return self._gateway.forward_unjudged(
                    tool=tool, args=args, actor=actor, session=session,
                    upstream=self._upstream,
                )
            # unmapped ⇒ deny, never pass-through (review note), and audit it.
            return self._gateway.refuse(
                reason=UNMAPPED_TOOL, resource=tool, action=None, data=args,
                actor=actor, session=session,
            )
        return self._gateway.submit(
            resource=mapping.resource,
            action=mapping.action,
            data=mapping.to_data(args),
            actor=actor,
            session=session,
        )


def interception_coverage_check(
    configured_endpoints: Sequence[str], *, gateway_endpoint: str
) -> None:
    """Fail startup if any configured tool endpoint bypasses the gateway.

    Interception coverage is only as good as the routing: a network path that does
    not terminate at the proxy is an uncovered escape hatch (design §1.2, review
    note). Raises ``CoverageError`` listing the offending endpoints.
    """
    strays = sorted(e for e in configured_endpoints if e != gateway_endpoint)
    if strays:
        raise CoverageError(
            f"tool endpoints bypass the gateway {gateway_endpoint!r}: {strays}"
        )
