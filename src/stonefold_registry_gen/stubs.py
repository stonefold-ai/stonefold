"""Handler-stub generation (implementation-plan Workstream G1).

The registry generator solved the blank-page problem for the *declaration*; the real
80% of adoption cost is the *code* behind it -- connectors, scope predicates, and
precondition checks. This module drafts that code from the same inputs:

* a SQL/OpenAPI/MCP **draft** -> a CRUD (SQL) or HTTP-dispatch connector stub, plus a
  scope-predicate stub for every tenancy/ownership column found;
* an existing **authoring registry** -> a signature stub for every declared connector,
  scope predicate, precondition check, and content hook (docs/06 sec. 5-6 signatures).

Same discipline as the registry generator (docs/06 sec. 9):

1. **Authoring-time only.** Nothing here is imported by the enforcement path; the
   output is a *draft* a human reviews, completes, and signs -- like a hand-written
   handler (docs/06 sec. 6 "registered functions are part of the trust surface").
2. **It looks like a draft.** Every generated body raises NotImplementedError under a
   TODO(review) marker, so an un-completed stub is loud, never silently trusted -- a
   raised handler is a *dependency failure* the gateway fails closed on (invariant 7),
   over-governed until implemented.
3. **The output is syntax-checked** (``validate_stub_code``) before it is written; a
   syntax error is a generator bug and blocks the write.

Generated text is pure ASCII (same convention as the registry drafts: it prints to
Windows cp1252 consoles).
"""

from __future__ import annotations

import ast
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from stonefold_registry_gen.kinds import pascal, split_words
from stonefold_registry_gen.model import DraftAction, DraftEntity, DraftRegistry

# Scope-key column -> the conventional predicate name + the actor attribute it reads
# (mirrors stonefold_core.scope.default_scope_registry). A reviewer confirms both.
_SCOPE_PREDICATES: dict[str, tuple[str, str]] = {
    "tenant_id": ("tenantOf", "claims['tenant']"),
    "owner_id": ("assignedToCurrentUser", "id"),
    "user_id": ("assignedToCurrentUser", "id"),
    "org_id": ("orgOf", "claims['org']"),
    "client_id": ("clientOf", "claims['client']"),
}


@dataclass
class ConnectorStub:
    name: str            # the registry connector name, e.g. "ledger-sql"
    type: str            # "sql" | "http" | "method" | ""  (drives CRUD vs dispatch)
    entities: list[str] = field(default_factory=list)  # entities it serves (SQL)


@dataclass
class StubPlan:
    """Everything the emitter needs: what code to draft and for which names."""

    domain: str
    source: str
    connectors: list[ConnectorStub] = field(default_factory=list)
    scope_predicates: list[tuple[str, str]] = field(default_factory=list)  # (name, actor_attr)
    precondition_checks: list[str] = field(default_factory=list)
    content_hooks: list[str] = field(default_factory=list)
    entities: list[DraftEntity] = field(default_factory=list)


# --------------------------------------------------------------------------
# builders
# --------------------------------------------------------------------------
def plan_from_draft(draft: DraftRegistry) -> StubPlan:
    """Plan the stubs implied by a freshly-drafted registry (SQL/OpenAPI/MCP).

    A SQL source drafts one CRUD connector serving every entity; an OpenAPI/MCP
    source drafts one HTTP-dispatch connector. Scope predicates come from the
    scope-key columns the importer flagged; preconditions/hooks are not derivable
    from a raw source, so those stubs come only via ``plan_from_registry``.
    """
    is_sql = draft.source == "sql"
    connector = ConnectorStub(
        name=f"{_ident(draft.domain)}-{'sql' if is_sql else 'api'}",
        type="sql" if is_sql else "http",
        entities=[e.name for e in draft.entities],
    )
    seen: dict[str, str] = {}
    for entity in draft.entities:
        for prop in entity.properties:
            if prop.scope_key and prop.name.lower() in _SCOPE_PREDICATES:
                name, actor_attr = _SCOPE_PREDICATES[prop.name.lower()]
                seen.setdefault(name, actor_attr)
    return StubPlan(
        domain=draft.domain, source=draft.source, connectors=[connector],
        scope_predicates=list(seen.items()), entities=list(draft.entities),
    )


def plan_from_registry(doc: Mapping[str, Any]) -> StubPlan:
    """Plan stubs from an existing authoring-format registry, one per *declared*
    name (docs/06 sec. 5): connectors, scopePredicates, preconditionChecks, hooks."""
    entities = _entities_from_authoring(doc.get("entities"))
    connectors_decl = doc.get("connectors")
    connectors: list[ConnectorStub] = []
    if isinstance(connectors_decl, Mapping):
        served = _entities_by_connector(doc.get("entities"))
        for name, decl in connectors_decl.items():
            ctype = str(decl.get("type", "")) if isinstance(decl, Mapping) else ""
            connectors.append(ConnectorStub(name=str(name), type=ctype,
                                            entities=served.get(str(name), [])))
    scope_predicates = [
        (str(n), _scope_actor_attr(str(n))) for n in _as_list(doc.get("scopePredicates"))
    ]
    return StubPlan(
        domain=str(doc.get("domain", "domain")), source="registry",
        connectors=connectors, scope_predicates=scope_predicates,
        precondition_checks=[str(n) for n in _as_list(doc.get("preconditionChecks"))],
        content_hooks=[str(n) for n in _as_list(doc.get("hooks"))],
        entities=entities,
    )


# --------------------------------------------------------------------------
# emitter
# --------------------------------------------------------------------------
_HEADER = '''\
"""DRAFT handler stubs generated by stonefold_registry_gen (source: {source}).

TODO(review): every function/method below raises NotImplementedError. Each is
SECURITY-CRITICAL code the gateway calls on matching actions (docs/06 sec. 6) -- a
reviewer MUST implement and sign each, and keep a conformance test per handler
(stonefold_gates.conformance). Until then every stub fails closed: a raised handler is a
dependency failure the gateway denies/halts on (invariant 7), so an un-completed
stub is over-governed, never a silent allow.

Nothing here is authoritative until reviewed. This file is authoring-time output,
not part of the enforcement path.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from stonefold_core.connector import Connector, ConnectorResult
from stonefold_core.models import Actor, ResolvedAction
from stonefold_core.scope import ScopePredicate
from stonefold_gates.base import GateContext

_TODO = "TODO(review): implement this handler before it governs anything"'''


def emit_stubs(plan: StubPlan) -> str:
    """Render the plan to a Python module of reviewable, fail-closed stubs."""
    blocks: list[str] = [_HEADER.format(source=plan.source)]
    for connector in plan.connectors:
        blocks.append(_connector_block(connector))
    if plan.scope_predicates:
        blocks.append("# --- scope predicates (RFC sec. 6.3; docs/06 sec. 6) ---")
        for name, actor_attr in plan.scope_predicates:
            blocks.append(_scope_block(name, actor_attr))
    if plan.precondition_checks:
        blocks.append("# --- precondition checks (docs/06 sec. 6; prefer stonefold_gates.stock) ---")
        for name in plan.precondition_checks:
            blocks.append(_precondition_block(name))
    if plan.content_hooks:
        blocks.append("# --- content hooks (contentCheck; True=clean/pass, False=block) ---")
        for name in plan.content_hooks:
            blocks.append(_hook_block(name))
    blocks.append(_wiring_block(plan))
    return "\n\n".join(blocks).rstrip("\n") + "\n"


def _connector_block(connector: ConnectorStub) -> str:
    cls = f"{pascal(split_words(connector.name))}Connector"
    served = ", ".join(connector.entities) or "(declare the entities it serves)"
    if connector.type == "sql":
        guide = [
            "        # SQL CRUD: dispatch on action.kind --",
            "        #   observe    -> SELECT ... WHERE <scope.sql_where(actor)> (scope below the model);",
            "        #   record     -> INSERT the action.data row;",
            "        #   transition -> UPDATE ... SET state = <to> WHERE id = target AND <scope>;",
            "        #   effect     -> perform + return ConnectorResult(receipt=..., result_refs=[...]).",
        ]
    else:
        guide = [
            "        # HTTP dispatch: map action.resource/action to a request; inject the scope",
            "        # as a MANDATORY filter (scope.query_param(actor)) -- never trust the agent",
            "        # to scope itself; return ConnectorResult(receipt=..., result_refs=[...]).",
        ]
    lines = [
        f"class {cls}:",
        f'    """{connector.type or "method"} connector for: {served}.',
        "",
        "    Implements the stonefold_core.connector.Connector protocol. Applies the INJECTED",
        "    scope as a real constraint (design sec. 5); holds NO policy logic (CLAUDE.md).",
        '    """',
        "",
        "    def execute(self, action: ResolvedAction, scope: ScopePredicate | None, "
        "actor: Actor) -> ConnectorResult:",
        *guide,
        f'        raise NotImplementedError(_TODO + ": {cls}.execute")',
        "",
        "    def dispatch(self, action: ResolvedAction, actor: Actor, "
        "idempotency_key: str) -> ConnectorResult:",
        "        # Dispatch a staged effect. MUST be idempotent on idempotency_key",
        "        # (a worker retry must never double-send).",
        f'        raise NotImplementedError(_TODO + ": {cls}.dispatch")',
        "",
        "    def fetch_target(self, action: ResolvedAction, scope: ScopePredicate | None, "
        "actor: Actor) -> Mapping[str, Any] | None:",
        "        # Resolve the effect's target UNDER scope; return None if it is not in the",
        "        # actor's scoped set (=> DENY before dispatch).",
        f'        raise NotImplementedError(_TODO + ": {cls}.fetch_target")',
        "",
        "    def cancel(self, handle: str) -> None:",
        "        # Abort an in-flight cancellable call (kill-switch); no-op if not cancellable.",
        f'        raise NotImplementedError(_TODO + ": {cls}.cancel")',
    ]
    return "\n".join(lines)


def _scope_block(name: str, actor_attr: str) -> str:
    cls = f"{pascal(split_words(name))}Scope"
    lines = [
        f"class {cls}:",
        f'    """Scope predicate {name!r} -- reads actor.{actor_attr}. Over-governed default:',
        "    selects NOTHING until implemented (an empty scope must never widen, RFC sec. 6.3).",
        f"    Consider stonefold_core.scope.AttributeScope({name!r}, <column>, <actor_attr>) instead.",
        '    """',
        "",
        f"    name: str = {name!r}",
        "",
        "    def matches(self, attrs: Mapping[str, Any], actor: Actor) -> bool:",
        f'        raise NotImplementedError(_TODO + ": {name} scope predicate")',
        "",
        "    def sql_where(self, actor: Actor) -> tuple[str, dict[str, Any]]:",
        '        # over-governed until implemented: "1 = 0" selects no rows.',
        f'        raise NotImplementedError(_TODO + ": {name}.sql_where")',
        "",
        "    def query_param(self, actor: Actor) -> tuple[str, Any]:",
        f'        raise NotImplementedError(_TODO + ": {name}.query_param")',
    ]
    return "\n".join(lines)


def _precondition_block(name: str) -> str:
    lines = [
        f"def {_ident(name)}(ctx: GateContext) -> bool:",
        f'    """Precondition check {name!r} -- return True to PASS, False to DENY.',
        "    MUST be pure/deterministic and fail closed: on a missing field or an",
        "    unparsable value return False, never raise for a *policy* verdict",
        "    (a raised exception is a dependency failure -> failureMode).",
        '    """',
        f'    raise NotImplementedError(_TODO + ": {name} precondition")',
    ]
    return "\n".join(lines)


def _hook_block(name: str) -> str:
    lines = [
        f"def {_ident(name)}(content: Mapping[str, Any]) -> bool:",
        f'    """Content hook {name!r} -- return True for clean/pass, False to block.',
        "    Deterministic verdict; may call an external DLP/moderation service.",
        '    """',
        f'    raise NotImplementedError(_TODO + ": {name} content hook")',
    ]
    return "\n".join(lines)


def _wiring_block(plan: StubPlan) -> str:
    conns = ", ".join(f"{c.name!r}: {pascal(split_words(c.name))}Connector()" for c in plan.connectors)
    scopes = ", ".join(f"{n!r}: {pascal(split_words(n))}Scope()" for n, _ in plan.scope_predicates)
    preconds = ", ".join(f"{n!r}: {_ident(n)}" for n in plan.precondition_checks)
    hooks = ", ".join(f"{n!r}: {_ident(n)}" for n in plan.content_hooks)
    lines = [
        "# --- wiring (fill in and register with the gateway) ---",
        "def wire_handlers() -> dict[str, Any]:",
        '    """Example registration. TODO(review): wire these into the gateway build',
        "    (Connectors({...}), ScopeRegistry({...}), preconditions={...}, hooks).\"\"\"",
        "    return {",
        f"        'connectors': {{{conns}}},",
        f"        'scope_predicates': {{{scopes}}},",
        f"        'precondition_checks': {{{preconds}}},",
        f"        'content_hooks': {{{hooks}}},",
        "    }",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# helpers + validation
# --------------------------------------------------------------------------
def _ident(name: str) -> str:
    """A valid Python identifier fragment from a registered name (``dlp.basic`` ->
    ``dlp_basic``). A leading digit is prefixed with ``_``."""
    out = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in name)
    if out and out[0].isdigit():
        out = "_" + out
    return out or "_handler"


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _scope_actor_attr(name: str) -> str:
    for _col, (pred, attr) in _SCOPE_PREDICATES.items():
        if pred == name:
            return attr
    return "id"


def _entities_from_authoring(entities: Any) -> list[DraftEntity]:
    out: list[DraftEntity] = []
    if not isinstance(entities, Mapping):
        return out
    for ename, edef in entities.items():
        actions: list[DraftAction] = []
        adefs = edef.get("actions") if isinstance(edef, Mapping) else None
        if isinstance(adefs, Mapping):
            for aname, adef in adefs.items():
                kind = str(adef.get("kind", "effect")) if isinstance(adef, Mapping) else "effect"
                actions.append(DraftAction(name=str(aname), kind=kind))
        out.append(DraftEntity(name=str(ename), actions=actions))
    return out


def _entities_by_connector(authoring: Any) -> dict[str, list[str]]:
    served: dict[str, list[str]] = {}
    if not isinstance(authoring, Mapping):
        return served
    for ename, edef in authoring.items():
        if isinstance(edef, Mapping):
            ds = edef.get("dataSource")
            if isinstance(ds, str):
                served.setdefault(ds, []).append(str(ename))
    return served


def validate_stub_code(text: str) -> list[str]:
    """Return syntax problems in the emitted stub module (empty = valid)."""
    try:
        ast.parse(text)
    except SyntaxError as exc:
        return [f"generated stub is not valid Python: {exc}"]
    return []
