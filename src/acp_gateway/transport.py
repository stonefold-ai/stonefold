"""Transports and the single chokepoint (design §0, §1; RFC §3).

The most important implementation fact (design §0) is that **the gateway is the
only path from the agent to any connector**. ``Gateway`` makes that concrete: it
holds the injected enforcement dependencies and exposes ``submit`` *once*, so
every transport routes through the same ``enforce`` call and none can diverge.

Two transports sit in front of it (RFC §3):

* **SIF-native** (design §1.1): the agent gets exactly one tool, ``submit_intent``,
  whose schema is generated from the registry (resource/action enums injected).
  Coverage is structural — there is no other tool, so there is no other path.
* **Interception / MCP proxy** (design §1.2): the agent keeps its tools, but each
  call is mapped to an ACP action and enforced. The mapping is the coverage
  boundary, so: an **unmapped tool denies** (never pass-through, review note),
  and a **free-form-string pass-through requires explicit acknowledgement**
  (review note). ``interception_coverage_check`` fails startup if any configured
  tool endpoint bypasses the gateway.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from acp_core import (
    Actor,
    AuditSink,
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
)
from acp_core.scope import ScopeResolver


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
        """The single enforcement entry point shared by both transports."""
        raw = RawCall(resource=resource, action=action, data=dict(data or {}))
        env = self._env_factory(raw) if self._env_factory is not None else self._env
        return enforce(
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
            agent=self._agent,
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
        result = EvalResult(decision=Decision.DENY, rule=reason)
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


# --- SIF-native: the single generated tool (design §1.1) ------------------
def submit_intent_schema(registry: InMemoryRegistry) -> dict[str, Any]:
    """Generate the ``submit_intent`` tool schema from the registry.

    The agent can name only declared resources/actions (enum-injected), so it can
    emit nothing the registry doesn't know — the structural-coverage property.
    ``x-acp-actions`` carries the resource→actions catalogue for richer clients.
    """
    catalogue = {
        name: sorted(rdef.actions) for name, rdef in registry.file.resources.items()
    }
    return {
        "name": "submit_intent",
        "description": (
            "Submit one intended action for enforcement. The gateway validates it "
            "against policy, injects scope, runs the gates, and either executes, "
            "stages, holds, or refuses it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "resource": {"type": "string", "enum": sorted(catalogue)},
                "action": {"type": "string"},
                "data": {"type": "object"},
            },
            "required": ["resource", "action"],
            "additionalProperties": False,
        },
        "x-acp-actions": catalogue,
    }


class SifNativeTransport:
    """The SIF-native executor of the one tool (design §1.1). It *is* the tool's
    implementation: every ``submit_intent`` call routes straight to the gateway."""

    def __init__(self, gateway: Gateway) -> None:
        self._gateway = gateway

    @property
    def tool_schema(self) -> dict[str, Any]:
        return submit_intent_schema(self._gateway.registry)

    def submit_intent(
        self, payload: Mapping[str, Any], *, actor: Actor, session: Session
    ) -> EvalResult:
        return self._gateway.submit(
            resource=str(payload.get("resource", "")),
            action=payload.get("action"),
            data=payload.get("data") or {},
            actor=actor,
            session=session,
        )


# --- Interception / MCP proxy (design §1.2) -------------------------------
class CoverageError(RuntimeError):
    """Startup coverage failure (design §1 review notes): an agent has a tool path
    that does not pass through the gateway, or an unacknowledged free-form
    pass-through tool. The gateway MUST refuse to start (invariant: no tool is
    reachable except through the gateway)."""


@dataclass(frozen=True)
class ToolMapping:
    """Maps an intercepted tool call to an ACP action (design §1.2).

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
