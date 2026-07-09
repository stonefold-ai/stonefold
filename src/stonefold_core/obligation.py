"""Obligation registries — the systems of record ``requireMatch`` matches
against (RFC §7.16 / CS-032, docs/06 §5b / CS-034).

This module is pure (no I/O — trust kernel): it declares the value types that
cross the gate↔adapter boundary, the four-operation adapter ``Protocol`` the
connector behind an obligation registry implements, and the pydantic model for
the registry *declaration* (connector, digest pin, capability, typed match
surface). The in-memory reference adapter lives in ``stonefold_store``; the gate
that consumes all of this lives in ``stonefold_gates``.

Stonefold never stores, owns, or edits obligations — the declaration names the
source and types the fields the policy compares; the gateway matches against the
adapter's response and (CS-035, Phase 6) consumes from it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

# Declared property types a tolerance clause may apply to (§13 rule 14:
# "tolerance applies to numeric/money fields only").
NUMERIC_TYPES = frozenset({"int", "integer", "number", "float", "decimal", "money"})

# Sentinel for "this path does not exist on the record" — distinct from an
# explicit ``None`` value only in that neither ever matches (CS-032 rule 4:
# absent OR null fails closed / excludes the record).
MISSING: Any = object()


def lookup_field(fields: Mapping[str, Any], path: str) -> Any:
    """Resolve a dotted record-relative path (``line.amount``) against one
    obligation's fields. Returns ``MISSING`` when any segment is absent."""
    value: Any = fields
    for part in path.split("."):
        if isinstance(value, Mapping) and part in value:
            value = value[part]
        else:
            return MISSING
    return value


def values_equal(a: Any, b: Any) -> bool:
    """Equality with the condition language's numeric coercion (RFC §8): the
    adapter's ``Eq`` filtering and the gate's clause re-evaluation must agree,
    or a record could match the query and fail the re-check spuriously."""
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a == b)
    na = _as_number(a)
    nb = _as_number(b)
    if na is not None and nb is not None:
        return na == nb
    return bool(a == b)


def _as_number(v: Any) -> float | None:
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    return None


# --------------------------------------------------------------------------
# Values crossing the gate ↔ adapter boundary
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Obligation:
    """One typed record from the system of record, as seen by the match:
    ``ref`` is the adapter's stable identifier for the obligation (or the
    consumable line within it); ``fields`` is the declared match surface."""

    ref: str
    fields: Mapping[str, Any]


@dataclass(frozen=True)
class EqConstraint:
    """One conjunct of the typed selector (§7.16 semantics 1): the record's
    ``field`` (dotted, record-relative) must equal ``value`` — the intent-side
    operand the gateway resolved at decision time. A pointer (CS-036) is just
    an ``EqConstraint`` on the record's id field: it narrows the query and the
    full ``match`` conjunction still evaluates against the re-read record."""

    field: str
    value: Any


# The typed selector the gateway derives from the ``match`` conjunction's
# equality clauses. Tolerance and non-equality clauses are evaluated by the
# GATE against the unique candidate, not pushed to the adapter.
Selector = tuple[EqConstraint, ...]


class ReserveOutcome(str, Enum):
    """Result of ``reserve(ref, intent_id)`` (CS-034; idempotent per pair)."""

    RESERVED = "reserved"
    ALREADY_RESERVED = "already_reserved"  # held by a DIFFERENT intent
    ALREADY_CONSUMED = "already_consumed"


class ConsumeOutcome(str, Enum):
    CONSUMED = "consumed"
    ALREADY_CONSUMED = "already_consumed"  # spent by a DIFFERENT intent


class ReleaseOutcome(str, Enum):
    RELEASED = "released"
    NOT_HELD = "not_held"  # idempotent no-op (incl. an already-expired reservation)


@dataclass(frozen=True)
class ConsumeResult:
    """``consume``'s receipt (CS-035/CS-037): a retry by the SAME intent is
    idempotent and returns the original receipt id."""

    outcome: ConsumeOutcome
    receipt: str | None = None


class ObligationRegistry(Protocol):
    """The four-operation adapter contract behind a declared obligation
    registry (docs/06 §5b). All four are idempotent per (ref, intent id).
    Phase 5 (decision-time matching) calls ``query`` only; the reservation
    lifecycle (CS-035) wires the other three at staging/dispatch/settle."""

    def query(self, selector: Selector) -> Sequence[Obligation]: ...

    def reserve(self, ref: str, intent_id: str) -> ReserveOutcome: ...

    def consume(self, ref: str, intent_id: str) -> ConsumeResult: ...

    def release(self, ref: str, intent_id: str) -> ReleaseOutcome: ...


# --------------------------------------------------------------------------
# The registry DECLARATION (docs/06 §5b — YAML, reviewable)
# --------------------------------------------------------------------------
class Capability(str, Enum):
    """How ``consume`` composes with the effect's settlement (CS-034/CS-035):
    ``transactional`` — inside the same transaction as the effect's commit and
    the audit write; ``window`` — immediately after connector confirmation,
    with the residual window surfaced in the audit record."""

    TRANSACTIONAL = "transactional"
    WINDOW = "window"


class ObligationProperty(BaseModel):
    """One declared field of the match surface (docs/06 §3 forms: ``type``,
    ``values``, nested ``properties``). Only declared, typed fields participate
    in matching — free text is never a match input."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: str | None = None
    values: tuple[str, ...] | None = None
    properties: dict[str, "ObligationProperty"] = Field(default_factory=dict)


class ObligationRegistryDecl(BaseModel):
    """A declared obligation registry (docs/06 §5b). ``schema`` is the match
    surface, not a domain model: only the fields policies compare and consume.
    ``digest`` pins the adapter connector's artifact (CS-020 applies — the
    registry loader merges it into the connector digest map)."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    connector: str
    digest: str | None = None
    capability: Capability
    fields: dict[str, ObligationProperty] = Field(alias="schema")

    def property_at(self, path: str) -> ObligationProperty | None:
        """The declared property at a dotted record-relative path (``None``
        when any segment is undeclared) — §13 rule 14's existence check."""
        props = self.fields
        prop: ObligationProperty | None = None
        for part in path.split("."):
            prop = props.get(part)
            if prop is None:
                return None
            props = prop.properties
        return prop

    def has_path(self, path: str) -> bool:
        return self.property_at(path) is not None

    def is_numeric(self, path: str) -> bool:
        """Whether the declared type at ``path`` admits a tolerance clause
        (§13 rule 14). A ``values`` enum or an object node is never numeric."""
        prop = self.property_at(path)
        return (
            prop is not None
            and prop.values is None
            and not prop.properties
            and prop.type is not None
            and prop.type in NUMERIC_TYPES
        )
