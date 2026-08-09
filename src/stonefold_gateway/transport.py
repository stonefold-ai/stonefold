# SPDX-License-Identifier: Apache-2.0
"""Transports and the single chokepoint (design §0, §1; RFC §3).

The most important implementation fact (design §0) is that **the gateway is the
only path from the agent to any connector**. ``Gateway`` makes that concrete: it
holds the injected enforcement dependencies and exposes ``submit`` *once*, so
every transport routes through the same ``enforce`` call and none can diverge.

Two transports sit in front of it (RFC §3):

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
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from stonefold_core import (
    Actor,
    AuditSink,
    BatchResult,
    CompiledPolicy,
    Connectors,
    Decision,
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
from stonefold_core.feedback import agent_view, agent_view_batch
from stonefold_core.freshness import FreshnessConfig
from stonefold_core.obligation import ObligationRegistry
from stonefold_core.scope import ScopeResolver


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
        # Decision freshness (v0.4 CS-017), opt-in: with a config every staged
        # effect gets a TTL stamped from the request env's clock.
        self._freshness = freshness
        # v0.6 (CS-035): the obligation-registry adapters the commit phase
        # reserves/consumes from — the same map the gate engine matches against.
        self._obligations = obligations
        # v0.6 (CS-031): the deployment's hold-dedupe window (seconds); ``None``
        # disables collapsing — deployment configuration, like decision TTLs.
        self._dedupe_window_s = dedupe_window_s
        self._agent = policy.agent if policy is not None else agent

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

        The returned result is the AGENT's view (CS-030): redacted to the
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
        )
        return agent_view(result)

    def submit_batch(
        self,
        operations: Sequence[Mapping[str, Any]],
        *,
        actor: Actor,
        session: Session,
    ) -> BatchResult:
        """Enforce a SIF batch atomically (RFC §12, CS-023; SIF §5).

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
        used by the proxy for an unmapped tool (design §1.2: unmapped ⇒ deny)."""
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
            )
        )
        return result


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
        atomically per RFC §12 / CS-023."""
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
    """A tool/MCP reverse proxy in front of the gateway (design §1.2)."""

    def __init__(
        self,
        gateway: Gateway,
        mappings: Iterable[ToolMapping],
        *,
        acknowledge_freeform: bool = False,
    ) -> None:
        self._gateway = gateway
        self._by_tool: dict[str, ToolMapping] = {m.tool: m for m in mappings}
        unacknowledged = sorted(
            m.tool for m in self._by_tool.values() if m.free_form and not acknowledge_freeform
        )
        if unacknowledged:
            raise CoverageError(
                "free-form pass-through tools require explicit acknowledgement: "
                f"{unacknowledged}"
            )

    def call_tool(
        self, tool: str, args: Mapping[str, Any], *, actor: Actor, session: Session
    ) -> EvalResult:
        mapping = self._by_tool.get(tool)
        if mapping is None:
            # unmapped ⇒ deny, never pass-through (review note), and audit it.
            return self._gateway.refuse(
                reason="unmapped-tool", resource=tool, action=None, data=args,
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
