# SPDX-License-Identifier: Apache-2.0
"""Scope injection — enforcement *below the model* (RFC §6.3, design §5).

The agent's intent carries **no** scope; the gateway derives a named, registered
``ScopePredicate`` from the *actor's* identity (from the session, never the
payload — invariant 3) and the connector applies it as a real constraint:

* a read (``observe``) ⇒ an appended SQL ``WHERE`` / an in-memory row filter;
* an effect ⇒ a **pre-resolution authorization check** (review note, design §5):
  the target must be visible under the actor's scope, else DENY before dispatch.

This module is pure: it computes the predicate and how to realise it, but does no
I/O — the connectors (``stonefold_connectors``) perform the filtered access.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from stonefold_core.compiler import CompiledPolicy
    from stonefold_core.models import Actor


class ScopePredicate(Protocol):
    """A named constraint a connector applies below the gateway. Structural so a
    connector can realise it without knowing the concrete class."""

    @property
    def name(self) -> str:
        """The registered predicate name (for the audit's ``scopeApplied``)."""
        ...

    def matches(self, attrs: Mapping[str, Any], actor: "Actor") -> bool:
        """True if a row/target with these attributes is in the actor's set."""
        ...

    def sql_where(self, actor: "Actor") -> tuple[str, dict[str, Any]]:
        """A ``(clause, params)`` pair to AND into a SQL ``WHERE`` (psycopg
        ``%(name)s`` placeholders)."""
        ...

    def query_param(self, actor: "Actor") -> tuple[str, Any]:
        """A mandatory ``(name, value)`` filter for an HTTP/REST connector."""
        ...


@dataclass(frozen=True)
class AttributeScope:
    """The POC scope predicate: a single attribute of the row/target must equal a
    value derived from the actor (its id or a named claim).

    STONEFOLD-AMBIGUITY (RFC §6.3): the registry declares predicate *names* only; their
    column/claim binding is a gateway concern. These bindings are the gateway's
    registered implementations — a real deployment plugs richer predicates (an
    OPA/IAM seam) behind the same ``ScopePredicate`` protocol.
    """

    name: str
    column: str  # the row/target attribute, e.g. "owner_id"
    actor_attr: str  # "id" ⇒ actor.id; otherwise a key in actor.claims

    def actor_value(self, actor: "Actor") -> Any:
        if self.actor_attr == "id":
            return actor.id
        return actor.claims.get(self.actor_attr)

    def is_empty(self, actor: "Actor") -> bool:
        """The actor resolves to no scope ⇒ matching actions return empty / are
        refused, never widened (RFC §6.3)."""
        return self.actor_value(actor) is None

    def matches(self, attrs: Mapping[str, Any], actor: "Actor") -> bool:
        if self.is_empty(actor):
            return False
        return bool(attrs.get(self.column) == self.actor_value(actor))

    def sql_where(self, actor: "Actor") -> tuple[str, dict[str, Any]]:
        param = f"scope_{self.column}"
        if self.is_empty(actor):
            # an empty scope must select nothing, never everything.
            return "1 = 0", {}
        return f"{self.column} = %({param})s", {param: self.actor_value(actor)}

    def query_param(self, actor: "Actor") -> tuple[str, Any]:
        return self.column, self.actor_value(actor)


class ScopeRegistry:
    """The gateway's registered scope predicates, keyed by the name a policy
    references (RFC §6.3: predicates are registered, not free expressions)."""

    def __init__(self, predicates: Mapping[str, ScopePredicate]) -> None:
        self._predicates = dict(predicates)

    def get(self, name: str) -> ScopePredicate | None:
        return self._predicates.get(name)

    def has(self, name: str) -> bool:
        return name in self._predicates


def default_scope_registry() -> ScopeRegistry:
    """POC bindings for every predicate the example registry declares."""
    return ScopeRegistry(
        {
            "assignedToCurrentUser": AttributeScope("assignedToCurrentUser", "owner_id", "id"),
            "customerAssignedToCurrentUser": AttributeScope("customerAssignedToCurrentUser", "owner_id", "id"),
            "tenantOf": AttributeScope("tenantOf", "tenant_id", "tenant"),
            "clientOf": AttributeScope("clientOf", "client_id", "client"),
            "forMatterOfClient": AttributeScope("forMatterOfClient", "client_id", "client"),
            "inWard": AttributeScope("inWard", "ward", "ward"),
            "forPatientInWard": AttributeScope("forPatientInWard", "ward", "ward"),
            "inCompartment": AttributeScope("inCompartment", "compartment", "clearance"),
        }
    )


class ScopeResolver:
    """Maps a resource to its registered scope predicate, using the policy's
    ``scope`` block + the gateway's predicate registry."""

    def __init__(self, scope_map: Mapping[str, str], registry: ScopeRegistry) -> None:
        self._scope_map = dict(scope_map)
        self._registry = registry

    def scope_for(self, resource: str) -> ScopePredicate | None:
        ref = self._scope_map.get(resource)
        if ref is None:
            return None
        # values may be a call form, e.g. ``tenantOf(actor)`` — the registered
        # name is the part before '(' (mirrors the linter, §13.1).
        name = ref.split("(", 1)[0].strip()
        return self._registry.get(name)


def make_scope_resolver(
    policy: "CompiledPolicy", registry: ScopeRegistry | None = None
) -> ScopeResolver:
    return ScopeResolver(policy.policy.scope, registry or default_scope_registry())
