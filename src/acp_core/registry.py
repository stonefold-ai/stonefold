"""The model registry (design §2, RFC §2/§12 step 1).

The registry is the declared catalogue of resources and actions the gateway
knows about — loaded once at startup into an indexed in-memory structure.
Resolution of an attempted action is an O(1) map lookup; an **unknown
resource/action short-circuits to DENY** (by raising ``UnknownActionError``)
before any policy runs. This module is pure (no I/O beyond reading a provided
mapping) and is part of the trust kernel.

The registry also declares the *named* extension points the frozen vocabulary
hangs off (RFC §13.1): scope predicates, content-check hooks, precondition
checks, named sets, and disclosure sinks. They are parsed here so M1's linter
can check that every name a policy references exists.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from acp_core.enums import (
    Emission,
    Explainability,
    Kind,
    OperativeForce,
    Reversibility,
)
from acp_core.models import Attributes, Compensation, RawCall, ResolvedAction


class UnknownActionError(Exception):
    """Raised when a ``RawCall`` names a resource or action not in the registry.

    Per RFC §12 step 1, the pipeline turns this into a DENY (rule
    ``unknown-action``) and audits it.
    """


class ActionDef(BaseModel):
    """A declared action: its kind, governance attributes, and (for a
    transition) its legal from-states (RFC §3–§5, §4.5)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Kind
    reversibility: Reversibility = Reversibility.REVERSIBLE
    emission: Emission = Emission.NONE
    operativeForce: OperativeForce = OperativeForce.NONE
    resultSensitivity: str = "internal"
    explainability: Explainability = Explainability.NONE
    # Legal source states for a TRANSITION action (RFC §4.5). Accepts the YAML
    # key ``from`` (a Python keyword) via alias.
    from_states: tuple[str, ...] = Field(default=(), alias="from")
    # Per-action connector override; falls back to the resource's connector.
    connector: str | None = None
    # Declared compensation for an irreversible effect (design §9).
    compensation: Compensation | None = None

    def attributes(self) -> Attributes:
        return Attributes(
            reversibility=self.reversibility,
            emission=self.emission,
            operativeForce=self.operativeForce,
            resultSensitivity=self.resultSensitivity,
            explainability=self.explainability,
        )


class ResourceDef(BaseModel):
    """A declared resource: its default connector and its named actions."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    connector: str = "in_memory"
    actions: dict[str, ActionDef] = Field(default_factory=dict)


class RegistryFile(BaseModel):
    """The full on-disk registry document."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    resources: dict[str, ResourceDef] = Field(default_factory=dict)
    connectors: tuple[str, ...] = ()
    scopePredicates: tuple[str, ...] = ()
    contentHooks: tuple[str, ...] = ()
    preconditionChecks: tuple[str, ...] = ()
    sets: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    sinks: tuple[str, ...] = ()


class Registry(Protocol):
    """Structural interface the pipeline depends on (design §2)."""

    def resolve(self, call: RawCall) -> ResolvedAction:
        """Resolve a raw call to a typed action, or raise
        ``UnknownActionError`` for an unknown resource/action."""
        ...


class InMemoryRegistry:
    """The default registry: an indexed view over a ``RegistryFile``."""

    def __init__(self, data: RegistryFile) -> None:
        self._data = data

    @property
    def file(self) -> RegistryFile:
        return self._data

    def resolve(self, call: RawCall) -> ResolvedAction:
        resource = self._data.resources.get(call.resource)
        if resource is None:
            raise UnknownActionError(f"unknown resource: {call.resource!r}")
        if call.action is None:
            raise UnknownActionError(
                f"no action named on resource {call.resource!r}"
            )
        action = resource.actions.get(call.action)
        if action is None:
            raise UnknownActionError(
                f"unknown action {call.action!r} on resource {call.resource!r}"
            )
        return ResolvedAction(
            kind=action.kind,
            resource=call.resource,
            action=call.action,
            data=dict(call.data),
            attrs=action.attributes(),
            connector=action.connector or resource.connector,
            from_states=action.from_states,
            compensation=action.compensation,
        )

    # --- registry introspection used by the linter (M1) and gates (M2) ---

    def has_scope_predicate(self, name: str) -> bool:
        return name in self._data.scopePredicates

    def has_content_hook(self, name: str) -> bool:
        return name in self._data.contentHooks

    def has_precondition_check(self, name: str) -> bool:
        return name in self._data.preconditionChecks

    def has_named_set(self, name: str) -> bool:
        return name in self._data.sets

    def named_set(self, name: str) -> tuple[str, ...]:
        return self._data.sets.get(name, ())

    def has_sink(self, name: str) -> bool:
        return name in self._data.sinks

    def action_def(self, resource: str, action: str) -> ActionDef | None:
        res = self._data.resources.get(resource)
        if res is None:
            return None
        return res.actions.get(action)

    def actions_of_kind(self, resource: str, kind: Kind) -> tuple[str, ...]:
        res = self._data.resources.get(resource)
        if res is None:
            return ()
        return tuple(
            name for name, a in res.actions.items() if a.kind == kind
        )


def load_registry(data: dict[str, object]) -> InMemoryRegistry:
    """Build a registry from an already-parsed mapping (e.g. ``yaml.safe_load``).

    Kept I/O-free so ``acp_core`` stays pure; callers read the file.
    """

    return InMemoryRegistry(RegistryFile.model_validate(data))
