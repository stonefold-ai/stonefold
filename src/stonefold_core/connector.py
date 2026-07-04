"""The connector seam (design §2, §5; RFC §12 step 6).

A connector is the *only* thing that touches an external system, and it does two
jobs and no policy: it **executes** a resolved action (applying the injected
scope filter) and it **fetches a target** under scope for the effect
pre-resolution check. Policy logic never lives in a connector (CLAUDE.md).

``stonefold_core`` declares the protocol and the result value type; concrete adapters
(in-memory / SQL / HTTP / email) live in ``stonefold_connectors`` and are injected into
``enforce`` through ``ConnectorRegistry`` — the kernel never imports them.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from stonefold_core.models import Actor, ResolvedAction
from stonefold_core.scope import ScopePredicate


class ConnectorCancelled(Exception):
    """Raised by a connector when an in-flight cancellable call is aborted by the
    kill-switch (design §8.5). The dispatch worker maps it to a terminal
    ``CANCELLED`` settle — distinct from an ordinary dispatch ``FAILED``."""


# The settle reason for an effect refused because the scope predicate no longer
# selects its target at dispatch/commit time (v0.4 CS-018, acceptance B4/B5).
SCOPE_LOST = "scope-lost"


class ScopeLostError(Exception):
    """Raised by a *transactional* connector when the scope predicate,
    re-asserted inside the effect's own transaction, no longer selects the
    target (CS-018): the write affected zero rows and was **not** committed.
    The dispatch worker settles the row ``FAILED`` with reason ``scope-lost`` —
    the effect lands on authorized state or not at all, never partially."""


class ScopeReassertion(str, Enum):
    """How a connector closes (or prices) the decide→commit scope race (CS-018)."""

    TRANSACTIONAL = "transactional"  # predicate carried into the effect's own tx
    WINDOW = "window"  # residual race window remains; declared, re-checked pre-dispatch


class ScopeCapability(BaseModel):
    """A connector's declared scope-reassertion capability (CS-018) — connector
    metadata, declared once per connector implementation (like the gateway's
    scope-predicate bindings, this lives in gateway code, not in policy syntax).

    ``transactional``: the connector can AND the scope predicate into the
    effect's own transaction (SQL-class) — the gateway then guarantees B4.
    ``window``: it cannot (HTTP, email, device); ``window`` names the residual
    race window so the audit prices it rather than hiding it (B5).
    """

    model_config = ConfigDict(frozen=True)

    reassertion: ScopeReassertion
    window: str | None = None  # the declared residual window (WINDOW only)

    @model_validator(mode="after")
    def _pairing(self) -> "ScopeCapability":
        if self.reassertion is ScopeReassertion.WINDOW and not self.window:
            raise ValueError("a window connector must declare its residual window")
        if self.reassertion is ScopeReassertion.TRANSACTIONAL and self.window is not None:
            raise ValueError("a transactional connector has no residual window")
        return self

    @classmethod
    def transactional(cls) -> "ScopeCapability":
        return cls(reassertion=ScopeReassertion.TRANSACTIONAL)

    @classmethod
    def window_declared(cls, declared: str) -> "ScopeCapability":
        return cls(reassertion=ScopeReassertion.WINDOW, window=declared)

    def audit_note(self) -> str:
        """The ``scopeApplied`` entry recording which reassertion form ran."""
        if self.reassertion is ScopeReassertion.TRANSACTIONAL:
            return "reassertion:transactional"
        return f"reassertion:window:{self.window}"


def scope_capability_of(connector: object) -> ScopeCapability:
    """The connector's declared ``scope_capability``, additive and fail-safe: a
    connector that declares nothing is treated as having an *undeclared* residual
    window — the worker still re-resolves the target pre-dispatch and the audit
    prices the window as ``undeclared`` rather than pretending it is closed."""
    declared = getattr(connector, "scope_capability", None)
    if isinstance(declared, ScopeCapability):
        return declared
    return ScopeCapability(reassertion=ScopeReassertion.WINDOW, window="undeclared")


class ConnectorResult(BaseModel):
    """What a connector returns from ``execute`` (design §2)."""

    model_config = ConfigDict(frozen=True)

    kind: str  # "rows" | "receipt"
    rows: list[dict[str, Any]] = Field(default_factory=list)
    receipt: dict[str, Any] | None = None
    # The realised query/request (e.g. the SQL with the scope WHERE) — lets a
    # test assert that scope was injected *below* the model (B1).
    query: str | None = None
    handle: str | None = None  # in-flight cancellation handle (M5)
    # The downstream identifier(s) of the created/changed record(s) (RFC §11
    # resultRefs, CS-009). A connector that creates records SHOULD set this so the
    # audit log is actionable; the dispatch worker falls back to ``[receipt["id"]]``
    # when empty. A *list* because one dispatch may fan out to several records.
    result_refs: list[str] = Field(default_factory=list)


class Connector(Protocol):
    """Structural interface every adapter satisfies (design §2).

    CS-018 adds two *additive* pieces alongside this protocol: a declared
    ``scope_capability`` attribute (read via ``scope_capability_of``) and, for
    transactional connectors, the ``TransactionalDispatch`` surface below."""

    def execute(
        self, action: ResolvedAction, scope: ScopePredicate | None, actor: Actor
    ) -> ConnectorResult:
        """Run the action, applying ``scope`` as a real constraint below the
        gateway (design §5)."""
        ...

    def dispatch(
        self, action: ResolvedAction, actor: Actor, idempotency_key: str
    ) -> ConnectorResult:
        """Dispatch a staged effect (design §9). MUST be idempotent on
        ``idempotency_key`` so a worker retry never double-sends."""
        ...

    def fetch_target(
        self, action: ResolvedAction, scope: ScopePredicate | None, actor: Actor
    ) -> Mapping[str, Any] | None:
        """Resolve the effect's target under scope. ``None`` ⇒ the target is not
        in the actor's scoped set (DENY before dispatch, design §5)."""
        ...

    def cancel(self, handle: str) -> None:
        """Abort an in-flight cancellable call (used by the kill-switch, M5)."""
        ...


@runtime_checkable
class TransactionalDispatch(Protocol):
    """The extra dispatch surface of a connector that declared
    ``ScopeReassertion.TRANSACTIONAL`` (CS-018): dispatch with the scope
    predicate carried *into* the effect's own transaction. Zero rows selected
    by the re-asserted predicate ⇒ raise ``ScopeLostError`` (nothing committed).
    Additive: connectors that cannot do this simply don't implement it."""

    def dispatch_scoped(
        self,
        action: ResolvedAction,
        actor: Actor,
        idempotency_key: str,
        scope: ScopePredicate,
    ) -> ConnectorResult: ...


class ConnectorRegistry(Protocol):
    """Resolves a connector by the name the registry pinned on the action."""

    def get(self, name: str) -> Connector: ...


class Connectors:
    """A dict-backed ``ConnectorRegistry``. Unknown connector ⇒ ``KeyError``,
    which the pipeline turns into a fail-closed DENY (invariant 7)."""

    def __init__(self, connectors: Mapping[str, Connector]) -> None:
        self._connectors = dict(connectors)

    def get(self, name: str) -> Connector:
        return self._connectors[name]

    def has(self, name: str) -> bool:
        return name in self._connectors
