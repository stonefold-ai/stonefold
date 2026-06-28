"""The kill-switch value model and matching (RFC §9, design §8). Pure kernel.

A kill is **a flag, checked at the chokepoint, that turns matching actions into
an audited ``HALT`` and prevents any not-yet-dispatched effect from dispatching**
(design §8.1). This module declares the durable value types — ``KillScope``,
``KillOrder`` — and the deterministic matcher (``scope_matches`` / ``order_matches``).
It performs **no I/O**: the durable ``kill_orders`` table, the in-memory hot set,
and Redis propagation live behind the ``KillStore`` protocol, implemented in
``acp_store`` and injected into ``enforce`` (CLAUDE.md: keep ``acp_core`` pure).

The matcher is total and side-effect-free (invariant 1). An unevaluable kill
predicate matches — kill wins on ambiguity, never silently lets an action through
(invariant 7, fail closed).
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from acp_core.condition import EvalContext, evaluate, parse
from acp_core.enums import Kind
from acp_core.models import Actor, ResolvedAction, Session


class KillScopeKind(str, Enum):
    """What a kill order targets (design §8.2). Frozen — these are not policy
    vocabulary (invariant 8), they are operator controls."""

    GLOBAL = "global"  # every action on every agent
    AGENT = "agent"  # every action of one agent
    SESSION = "session"  # every action in one session
    ACTION_CLASS = "action_class"  # a (kind, resource, action) class of action


class KillScope(BaseModel):
    """The target selector of a ``KillOrder`` (design §8.2).

    For ``ACTION_CLASS`` any unset facet (``action_kind``/``resource``/``action``)
    is a wildcard, so ``action_class(resource="Payment")`` kills every action on
    ``Payment`` while ``action_class(action="pay")`` kills ``pay`` everywhere.
    """

    model_config = ConfigDict(frozen=True)

    kind: KillScopeKind
    agent: str | None = None
    session_id: str | None = None
    action_kind: Kind | None = None
    resource: str | None = None
    action: str | None = None

    @classmethod
    def for_global(cls) -> KillScope:
        return cls(kind=KillScopeKind.GLOBAL)

    @classmethod
    def for_agent(cls, agent: str) -> KillScope:
        return cls(kind=KillScopeKind.AGENT, agent=agent)

    @classmethod
    def for_session(cls, session_id: str) -> KillScope:
        return cls(kind=KillScopeKind.SESSION, session_id=session_id)

    @classmethod
    def for_action_class(
        cls,
        *,
        kind: Kind | None = None,
        resource: str | None = None,
        action: str | None = None,
    ) -> KillScope:
        if kind is None and resource is None and action is None:
            raise ValueError("an ACTION_CLASS kill must fix at least one facet")
        return cls(
            kind=KillScopeKind.ACTION_CLASS,
            action_kind=kind,
            resource=resource,
            action=action,
        )


class KillOrder(BaseModel):
    """A durable operator halt (design §8.2). Reversible via ``lifted_at``; the
    monotonic ``epoch`` lets a cached instance detect a missed invalidation and
    reload (design §8.9)."""

    model_config = ConfigDict(frozen=True)

    id: str
    scope: KillScope
    predicate: str | None = None  # optional extra §8 condition
    issued_by: str
    issued_at: datetime
    lifted_at: datetime | None = None
    epoch: int = 0

    @property
    def active(self) -> bool:
        return self.lifted_at is None


class KillTarget(BaseModel):
    """The projection of an attempted (or staged) action that a kill matches
    against. Built from the resolved action + the gateway-supplied identity —
    never from agent ``data`` for scope (invariant 3)."""

    model_config = ConfigDict(frozen=True)

    agent: str
    session_id: str
    kind: Kind | None = None
    resource: str | None = None
    action: str | None = None
    # frozen-grammar namespaces for an optional predicate (mirrors the gate
    # engine's context so a kill predicate reads the same ``action.*``/``data.*``
    # vocabulary).
    action_ns: dict[str, Any] = Field(default_factory=dict)
    actor_ns: dict[str, Any] = Field(default_factory=dict)
    data: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_resolved(
        cls, resolved: ResolvedAction, actor: Actor, session: Session, agent: str
    ) -> KillTarget:
        return cls(
            agent=agent,
            session_id=session.id,
            kind=resolved.kind,
            resource=resolved.resource,
            action=resolved.action,
            action_ns={
                "kind": resolved.kind.value,
                "name": resolved.action,
                "resource": resolved.resource,
                "reversibility": resolved.attrs.reversibility.value,
                "emission": resolved.attrs.emission.value,
                "operativeForce": resolved.attrs.operativeForce.value,
                "resultSensitivity": resolved.attrs.resultSensitivity,
                "explainability": resolved.attrs.explainability.value,
            },
            actor_ns={"id": actor.id, "roles": sorted(actor.roles), **actor.claims},
            data=dict(resolved.data),
        )

    @classmethod
    def from_pending(cls, pending: Any) -> KillTarget:
        """Build the target for a staged ``PendingAction`` (used by the dispatch
        kill check)."""
        return cls.from_resolved(
            pending.resolved, pending.actor, Session(id=pending.session_id), pending.agent
        )

    def to_eval_context(self) -> EvalContext:
        return EvalContext(
            namespaces={
                "action": self.action_ns,
                "data": self.data,
                "actor": self.actor_ns,
                "resource": {},
                "context": {},
            }
        )


def scope_matches(scope: KillScope, target: KillTarget) -> bool:
    """Pure structural match of a scope against a target (design §8.2)."""
    if scope.kind is KillScopeKind.GLOBAL:
        return True
    if scope.kind is KillScopeKind.AGENT:
        return scope.agent == target.agent
    if scope.kind is KillScopeKind.SESSION:
        return scope.session_id == target.session_id
    if scope.kind is KillScopeKind.ACTION_CLASS:
        # Each fixed facet must match; an un-resolved target (no kind/resource —
        # the top-of-pipeline pre-check) therefore never matches an ACTION_CLASS
        # order, which is matched at step 5 instead.
        if scope.action_kind is not None and scope.action_kind is not target.kind:
            return False
        if scope.resource is not None and scope.resource != target.resource:
            return False
        if scope.action is not None and scope.action != target.action:
            return False
        return True
    return False  # pragma: no cover - exhaustive above


def order_matches(order: KillOrder, target: KillTarget) -> bool:
    """Does an active ``order`` halt ``target``? Scope must match and, if a
    predicate is declared, it must evaluate true. A predicate that cannot be
    evaluated (parse error, missing path) **matches** — kill wins on ambiguity
    (invariant 7)."""
    if not order.active:
        return False
    if not scope_matches(order.scope, target):
        return False
    if order.predicate is None:
        return True
    try:
        return evaluate(parse(order.predicate), target.to_eval_context())
    except Exception:
        # fail closed: an unreadable kill condition halts rather than admits.
        return True


def is_killed(store: KillStore, target: KillTarget) -> bool:
    """Convenience over ``store.matches`` for callers that only need a bool."""
    return store.matches(target) is not None


class KillStore(Protocol):
    """The kill state, behind a seam (design §8.2). ``matches`` is the hot-path
    check the chokepoint calls; ``issue``/``lift`` are operator mutations;
    ``epoch`` exposes the monotonic counter for self-healing caches.

    Implementations: ``InMemoryKillStore`` (single instance), ``PostgresKillStore``
    (durable), ``CachedKillStore`` (hot set + pub/sub + epoch poll)."""

    def matches(self, target: KillTarget) -> KillOrder | None:
        """The first active order halting ``target``, or ``None``."""
        ...

    def issue(
        self, scope: KillScope, *, issued_by: str, predicate: str | None = None
    ) -> KillOrder: ...

    def lift(self, order_id: str) -> KillOrder: ...

    def active_orders(self) -> tuple[KillOrder, ...]: ...

    def epoch(self) -> int: ...
