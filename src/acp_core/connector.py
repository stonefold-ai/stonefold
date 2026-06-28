"""The connector seam (design §2, §5; RFC §12 step 6).

A connector is the *only* thing that touches an external system, and it does two
jobs and no policy: it **executes** a resolved action (applying the injected
scope filter) and it **fetches a target** under scope for the effect
pre-resolution check. Policy logic never lives in a connector (CLAUDE.md).

``acp_core`` declares the protocol and the result value type; concrete adapters
(in-memory / SQL / HTTP / email) live in ``acp_connectors`` and are injected into
``enforce`` through ``ConnectorRegistry`` — the kernel never imports them.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from acp_core.models import Actor, ResolvedAction
from acp_core.scope import ScopePredicate


class ConnectorCancelled(Exception):
    """Raised by a connector when an in-flight cancellable call is aborted by the
    kill-switch (design §8.5). The dispatch worker maps it to a terminal
    ``CANCELLED`` settle — distinct from an ordinary dispatch ``FAILED``."""


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


class Connector(Protocol):
    """Structural interface every adapter satisfies (design §2)."""

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
