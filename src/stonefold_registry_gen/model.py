"""Draft model for generated registries.

``stonefold_registry_gen`` is an AUTHORING-TIME tool (docs/06 §9): it drafts a
registry in the v1.x authoring format from artefacts the integrator already
has. Nothing here is imported by the enforcement path; the output is a draft
a human must review, complete, and sign before it governs anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DraftProperty:
    """One entity property / action data field in the draft."""

    name: str
    type: str  # int | decimal | boolean | dateTime | string
    required: bool = False
    hint: str | None = None  # rendered as a trailing ``# TODO(review)`` comment
    scope_key: bool = False  # a tenancy/ownership column ⇒ a scope-predicate stub


@dataclass
class DraftAction:
    """One declared action in the draft. ``kind`` is always a guess."""

    name: str
    kind: str  # observe | assess | record | effect | transition
    certain: bool = True  # False ⇒ the verb was unknown; kind defaulted
    verb: str = ""
    data: list[DraftProperty] = field(default_factory=list)
    suggested_reversibility: str | None = None
    hint: str | None = None


@dataclass
class DraftEntity:
    name: str
    properties: list[DraftProperty] = field(default_factory=list)
    actions: list[DraftAction] = field(default_factory=list)
    hint: str | None = None


@dataclass
class DraftRegistry:
    domain: str
    source: str  # "sql" | "openapi" | "mcp" — recorded in the header comment
    entities: list[DraftEntity] = field(default_factory=list)

    def entity(self, name: str) -> DraftEntity:
        """Get-or-create the entity with ``name`` (stable insertion order)."""
        for ent in self.entities:
            if ent.name == name:
                return ent
        ent = DraftEntity(name=name)
        self.entities.append(ent)
        return ent
