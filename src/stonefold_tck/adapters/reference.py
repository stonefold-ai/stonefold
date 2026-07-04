"""The reference implementation's TCK driver — the worked example (docs/12).

This is what an implementer writes: ~200 lines of test-only glue between the
TCK's ``ConformanceDriver`` contract and the gateway under test. It also
carries the authoring-format → loader-dialect registry converter, since the
TCK ships fixtures in the spec's authoring format (docs/06) while the
reference loader consumes its compact internal dialect (docs/03 → "Registry
dialects").
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from stonefold_core import (
    Actor,
    FreshnessConfig,
    InMemoryAuditSink,
    InMemoryRegistry,
    RequestEnv,
    Session,
    load_policy,
    load_registry,
)
from stonefold_core.connector import Connectors, ConnectorResult
from stonefold_core.digest import (
    DigestMismatchError,
    artifact_digest,
    assert_connector_digests,
)
from stonefold_core.kill import KillScope, KillScopeKind
from stonefold_core.linter import PolicyError
from stonefold_core.models import RawCall, ResolvedAction
from stonefold_core.scope import AttributeScope, ScopePredicate, ScopeRegistry, make_scope_resolver
from stonefold_gates.base import GateContext
from stonefold_gates.content import ContentHookRegistry
from stonefold_gates.engine import DefaultGateEngine, make_dispatch_revalidator
from stonefold_gateway.transport import Gateway
from stonefold_connectors import InMemoryConnector
from stonefold_store import DispatchWorker, InMemoryOutboxStore
from stonefold_store.kill_memory import InMemoryKillStore
from stonefold_tck.driver import (
    ALL_CAPABILITIES,
    AuditEntry,
    BatchSubmitResult,
    LoadResult,
    Operation,
    SubmitResult,
    TckActor,
)

_SCHEMA = Path(__file__).resolve().parents[3] / "spec" / "schema" / "stele.schema.json"

# The REQUIRED TCK freshness config (stonefold_tck.driver, CAP_FRESHNESS): the D5/D6
# checks pick their clock advances against exactly these TTLs.
_TCK_FRESHNESS = FreshnessConfig(
    default_ttl=timedelta(hours=24), irreversible_ttl=timedelta(minutes=30)
)


# --------------------------------------------------------------------------
# authoring format (docs/06) → reference loader dialect (docs/03)
# --------------------------------------------------------------------------
def authoring_to_compact(authoring: Mapping[str, Any]) -> dict[str, Any]:
    """Convert a registry in the v1.x authoring format to the reference
    loader's compact dialect. Adds the implicit ``read``/``create`` actions
    (doc 06 §4: observe/record are implicit per entity)."""
    resources: dict[str, Any] = {}
    for ename, edef in dict(authoring.get("entities") or {}).items():
        actions: dict[str, Any] = {}
        for aname, adef in dict(edef.get("actions") or {}).items():
            compact: dict[str, Any] = {"kind": adef["kind"]}
            for attr, value in dict(adef.get("attributes") or {}).items():
                compact[attr] = value
            for key in ("from", "compensation", "connector"):
                if adef.get(key) is not None:
                    compact[key] = adef[key]
            actions[aname] = compact
        actions.setdefault("read", {"kind": "observe"})
        actions.setdefault("create", {"kind": "record"})
        resources[ename] = {"connector": edef.get("dataSource"), "actions": actions}
    authoring_connectors = dict(authoring.get("connectors") or {})
    compact_extra: dict[str, Any] = {}
    # CS-024: a domain substituting its own classification labels declares them
    # as an ORDERED value set (docs/06 §4); carry the order across the bridge so
    # ``disclosure.maxClassification`` compares by it.
    value_sets = dict(authoring.get("valueSets") or {})
    if value_sets.get("resultSensitivity"):
        compact_extra["classifications"] = list(value_sets["resultSensitivity"])
    return {
        **compact_extra,
        "connectors": list(authoring_connectors.keys()),
        # CS-020: carry any pinned connector digests across the dialect bridge so
        # the compact loader can verify them (a no-op unless one is declared).
        "connector_digests": {
            name: decl["digest"]
            for name, decl in authoring_connectors.items()
            if isinstance(decl, Mapping) and decl.get("digest") is not None
        },
        "scopePredicates": list(authoring.get("scopePredicates") or []),
        "preconditionChecks": list(authoring.get("preconditionChecks") or []),
        "contentHooks": list(authoring.get("hooks") or []),
        "sinks": list(authoring.get("sinks") or []),
        "sets": {
            name: list(spec.get("values") or [])
            for name, spec in dict(authoring.get("namedSets") or {}).items()
        },
        "resources": resources,
    }


# --- the TCK's required registered-function semantics (docs/12 §3) ---------
def _tck_scope_registry() -> ScopeRegistry:
    return ScopeRegistry(
        {
            "tckOwnedBy": AttributeScope("tckOwnedBy", "owner_id", "id"),
            "tckTenantOf": AttributeScope("tckTenantOf", "tenant", "tenant"),
        }
    )


def _reject_marker(content: Mapping[str, Any]) -> bool:
    return "BLOCK-ME" not in str(dict(content))


def _flag_set(ctx: GateContext) -> bool:
    return bool(ctx.env.resource.get("flag"))


class _FailableConnector(InMemoryConnector):
    """The world connector, with TCK failure injection for D4. The injection
    hooks ``_dispatch`` so it fires on both dispatch forms (plain and the
    CS-018 ``dispatch_scoped``)."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_next: set[str] = set()

    def _dispatch(
        self, action: ResolvedAction, actor: Actor, idempotency_key: str,
        scope: ScopePredicate | None,
    ) -> ConnectorResult:
        if action.action in self.fail_next:
            self.fail_next.discard(action.action or "")
            raise RuntimeError(f"TCK-injected dispatch failure for {action.action!r}")
        return super()._dispatch(action, actor, idempotency_key, scope)


class ReferenceDriver:
    """``ConformanceDriver`` over the in-process reference stack."""

    implementation = "stonefold-reference (python)"

    def __init__(self) -> None:
        self._now: datetime | None = None
        self._sink: str | None = None
        self._context: dict[str, Any] = {}
        self._gateway: Gateway | None = None
        self._worker: DispatchWorker | None = None
        self._world: _FailableConnector = _FailableConnector()
        self._outbox: InMemoryOutboxStore | None = None
        self._kill: InMemoryKillStore = InMemoryKillStore()
        self._audit: InMemoryAuditSink = InMemoryAuditSink()
        self._registry: InMemoryRegistry | None = None
        self._connectors: Connectors | None = None

    # --- driver contract ---------------------------------------------------
    def capabilities(self) -> frozenset[str]:
        return ALL_CAPABILITIES

    def load(self, registry_yaml: str, policy_yaml: str) -> LoadResult:
        registry = load_registry(authoring_to_compact(yaml.safe_load(registry_yaml)))
        schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
        try:
            policy = load_policy(yaml.safe_load(policy_yaml), registry, schema=schema)
        except PolicyError as exc:
            return LoadResult(
                ok=False,
                errors=[f.message for f in exc.report.errors],
                warnings=[f.message for f in exc.report.warnings],
            )
        report = policy.lint_report
        warnings = [f.message for f in report.warnings] if report is not None else []

        # fresh state per load — the TCK relies on this reset
        self._now, self._sink, self._context = None, None, {}
        self._audit = InMemoryAuditSink()
        self._outbox = InMemoryOutboxStore(audit=self._audit)
        self._kill = InMemoryKillStore()
        self._world = _FailableConnector()
        connectors = Connectors({"tck-data": self._world, "tck-effects": self._world})
        self._connectors = connectors
        engine = DefaultGateEngine(
            registry,
            hooks=ContentHookRegistry({"tck.rejectMarker": _reject_marker}),
            preconditions={"tck.flagSet": _flag_set},
        )
        self._registry = registry
        scopes = make_scope_resolver(policy, _tck_scope_registry())
        self._gateway = Gateway(
            registry=registry,
            audit=self._audit,
            policy=policy,
            gates=engine,
            scopes=scopes,
            connectors=connectors,
            outbox=self._outbox,
            kill=self._kill,
            env_factory=self._env_factory,
            freshness=_TCK_FRESHNESS,  # v0.4 CS-017: TTL stamped at staging
            agent=policy.agent,
        )
        # CS-020: the load-time digest gate — a pinned connector that fails
        # verification refuses the load (fail closed), like a lint ERROR.
        try:
            assert_connector_digests(
                registry, connectors,
                failure_mode=policy.policy.defaults.failureMode,
                audit=self._audit, agent=policy.agent,
            )
        except DigestMismatchError as exc:
            return LoadResult(ok=False, errors=[str(exc)])
        # v0.4 wiring: the worker re-checks TTL + volatile gates inside the
        # claim (CS-017) and re-asserts scope at dispatch (CS-018).
        self._worker = DispatchWorker(
            self._outbox,
            connectors,
            registry=registry,
            kill=self._kill,
            clock=self._worker_clock,
            revalidate=make_dispatch_revalidator(engine, policy),
            scopes=scopes,
        )
        return LoadResult(ok=True, warnings=warnings)

    def set_clock(self, now: datetime) -> None:
        self._now = now

    def seed(self, resource: str, rows: Sequence[Mapping[str, Any]]) -> None:
        self._world.tables[resource] = [dict(r) for r in rows]

    def submit(self, actor: TckActor, session_id: str, op: Operation) -> SubmitResult:
        assert self._gateway is not None, "load() first"
        data = dict(op.data)
        if op.target is not None:
            data["id"] = op.target
        self._sink = op.sink
        self._context = dict(op.context)
        result = self._gateway.submit(
            resource=op.resource,
            action=op.action or "read",
            data=data,
            actor=Actor(id=actor.id, roles=set(actor.roles), claims=dict(actor.claims)),
            session=Session(id=session_id),
        )
        return SubmitResult(
            decision=result.decision.value, ticket=result.ticket,
            rows=self._rows_of(result.output), reason=result.rule,
        )

    @staticmethod
    def _rows_of(output: Any) -> Sequence[Mapping[str, Any]] | None:
        if output is None:
            return None
        rows: Sequence[Mapping[str, Any]] | None = getattr(output, "rows", None)
        if rows is None and isinstance(output, list):
            rows = output
        return rows

    def submit_batch(
        self, actor: TckActor, session_id: str, ops: Sequence[Operation]
    ) -> BatchSubmitResult:
        assert self._gateway is not None, "load() first"
        if ops:
            self._sink = ops[0].sink
            self._context = dict(ops[0].context)
        operations = []
        for op in ops:
            data = dict(op.data)
            if op.target is not None:
                data["id"] = op.target
            operations.append(
                {"resource": op.resource, "action": op.action or "read", "data": data}
            )
        batch = self._gateway.submit_batch(
            operations,
            actor=Actor(id=actor.id, roles=set(actor.roles), claims=dict(actor.claims)),
            session=Session(id=session_id),
        )
        return BatchSubmitResult(
            decision=batch.decision.value,
            failing_index=batch.failing_index,
            results=[
                SubmitResult(decision=r.decision.value, ticket=r.ticket,
                             rows=self._rows_of(r.output), reason=r.rule)
                for r in batch.results
            ],
        )

    def connector_digest(self, name: str) -> str:
        assert self._connectors is not None, "load() first"
        return artifact_digest(self._connectors.get(name))

    def tamper_connector(self, name: str) -> None:
        assert self._connectors is not None, "load() first"
        # Swap in an implementation from a DIFFERENT module (the plain
        # in-memory connector) so its artifact digest no longer matches the
        # pinned one — the reference pins module source bytes. Test-only glue,
        # so reaching into the registry's private map is deliberate: production
        # code has no swap hook, which is rather the point.
        self._connectors._connectors[name] = InMemoryConnector()

    def approve(self, ticket: str, approver_id: str) -> bool:
        assert self._outbox is not None
        try:
            self._outbox.approve(ticket, approver_id)
        except Exception:
            return False
        return True

    def reject(self, ticket: str, approver_id: str) -> bool:
        assert self._outbox is not None
        try:
            self._outbox.reject(ticket, approver_id)
        except Exception:
            return False
        return True

    def dispatch_once(self) -> int:
        assert self._worker is not None
        return self._worker.drain()

    def effects(self) -> Sequence[Mapping[str, Any]]:
        return [dict(e) for e in self._world.effects]

    def kill(
        self,
        *,
        scope: str,
        agent: str | None = None,
        session_id: str | None = None,
        resource: str | None = None,
        action: str | None = None,
        issued_by: str = "tck-operator",
    ) -> str:
        kill_scope = KillScope(
            kind=KillScopeKind(scope),
            agent=agent,
            session_id=session_id,
            resource=resource,
            action=action,
        )
        return self._kill.issue(kill_scope, issued_by=issued_by).id

    def lift(self, kill_id: str) -> None:
        self._kill.lift(kill_id)

    def audit(self) -> Sequence[AuditEntry]:
        return [
            AuditEntry(
                decision=r.decision.value,
                resource=r.resource,
                action=r.action,
                outcome=r.outcome,
                reason=r.rule or "",
            )
            for r in self._audit.records
        ]

    def inject_dispatch_failure(self, action: str) -> None:
        self._world.fail_next.add(action)

    def update_named_set(self, name: str, values: Sequence[str]) -> None:
        assert self._registry is not None, "load() first"
        self._registry.file.sets[name] = tuple(values)

    # --- internals -----------------------------------------------------------
    def _worker_clock(self) -> datetime:
        # the TCK's pinned clock; a wall clock only if a check forgot to pin it
        return self._now or datetime.now(timezone.utc)
    def _env_factory(self, raw: RawCall) -> RequestEnv:
        """Resolve the target row (by ``data.id``) so gates can read
        ``resource.*``; thread the injected clock, sink, and ambient context."""
        row: Mapping[str, Any] = {}
        target_id = raw.data.get("id")
        if target_id is not None:
            for candidate in self._world.tables.get(raw.resource, []):
                if str(candidate.get("id")) == str(target_id):
                    row = dict(candidate)
                    break
        return RequestEnv(
            resource=row, context=dict(self._context), now=self._now, sink=self._sink
        )
