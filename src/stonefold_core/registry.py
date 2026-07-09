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

from collections.abc import Mapping
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from stonefold_core.enums import (
    Emission,
    Explainability,
    Kind,
    OperativeForce,
    RetryClass,
    Reversibility,
)
from stonefold_core.models import Attributes, Compensation, RawCall, ResolvedAction
from stonefold_core.obligation import ObligationRegistryDecl


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


class PreconditionCheckDecl(BaseModel):
    """One declared precondition check (docs/06 §5, v0.6 CS-026/CS-029).

    The bare-name form declares a two-valued check whose codes default
    ``terminal``; the object form additionally declares hold capability and the
    retry class of every code the check may emit. ``holdCapable`` without
    ``reasonCodes`` is rejected at load (Stele §13 rule 18): every hold the
    check returned would be code-less and resolve fail.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    holdCapable: bool = False
    reasonCodes: dict[str, RetryClass] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _hold_capable_needs_codes(self) -> "PreconditionCheckDecl":
        if self.holdCapable and not self.reasonCodes:
            raise ValueError(
                f"check {self.name!r} declares holdCapable without reasonCodes "
                "(Stele §13 rule 18)"
            )
        return self


class RegistryFile(BaseModel):
    """The full on-disk registry document."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    resources: dict[str, ResourceDef] = Field(default_factory=dict)
    connectors: tuple[str, ...] = ()
    # CS-020: optional connector→digest pins. The loader accepts either the bare
    # name list above (no pins) or a map form (``{name: {digest: "sha256:…"}}``,
    # like the registry/v1.x authoring dialect); the map form's digests are split
    # out here so ``connectors`` stays a plain name tuple for every other consumer.
    connector_digests: dict[str, str] = Field(default_factory=dict)
    scopePredicates: tuple[str, ...] = ()
    contentHooks: tuple[str, ...] = ()
    preconditionChecks: tuple[str, ...] = ()
    # v0.6 (CS-026/CS-029): the full declarations behind the name list — the
    # loader accepts each ``preconditionChecks`` item as a bare name or an
    # object (``{name, holdCapable, reasonCodes}``) and splits them here, the
    # CS-020 ``connector_digests`` pattern. Every declared check has an entry
    # (bare names get the two-valued default).
    precondition_decls: dict[str, PreconditionCheckDecl] = Field(default_factory=dict)
    sets: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    sinks: tuple[str, ...] = ()
    # v0.6 (CS-034): the systems of record ``requireMatch`` matches against
    # (docs/06 §5b). A declared ``digest`` pins the adapter connector's artifact
    # — merged into ``connector_digests`` below so CS-020's load/dispatch
    # verification covers obligation adapters with no extra machinery.
    obligationRegistries: dict[str, ObligationRegistryDecl] = Field(
        default_factory=dict
    )
    # CS-024: the DECLARED ORDER of classification labels (lowest first) that
    # ``disclosure.maxClassification`` compares by. The default is the built-in
    # ``resultSensitivity`` order (RFC §7.12); a domain substituting its own
    # labels MUST declare them as an ordered value set (docs/06 §4 — order is
    # list position). A label missing from the order makes the gate fail closed.
    classifications: tuple[str, ...] = (
        "public",
        "internal",
        "confidential",
        "restricted",
    )

    @model_validator(mode="before")
    @classmethod
    def _split_connector_digests(cls, data: Any) -> Any:
        """Normalise a map-form ``connectors`` block into a name tuple plus a
        ``connector_digests`` map (CS-020). A list stays a list (no pins). An
        explicit ``connector_digests`` key is honoured and merged."""
        if not isinstance(data, dict):
            return data
        connectors = data.get("connectors")
        if isinstance(connectors, Mapping):
            digests = dict(data.get("connector_digests") or {})
            for name, decl in connectors.items():
                if isinstance(decl, Mapping) and decl.get("digest") is not None:
                    digests.setdefault(name, decl["digest"])
            data = {**data, "connectors": tuple(connectors.keys()),
                    "connector_digests": digests}
        return data

    @model_validator(mode="before")
    @classmethod
    def _merge_obligation_digests(cls, data: Any) -> Any:
        """Merge each obligation registry's declared adapter ``digest`` into the
        connector digest map (CS-034: "CS-020 pinning applies"). An explicit pin
        on the same connector wins; order relative to the connectors-map split
        is irrelevant because both merge over whatever is already present."""
        if not isinstance(data, dict):
            return data
        registries = data.get("obligationRegistries")
        if not isinstance(registries, Mapping):
            return data
        digests = dict(data.get("connector_digests") or {})
        for decl in registries.values():
            if isinstance(decl, Mapping) and decl.get("digest") and decl.get("connector"):
                digests.setdefault(str(decl["connector"]), str(decl["digest"]))
        return {**data, "connector_digests": digests}

    @model_validator(mode="after")
    def _obligation_connectors_declared(self) -> "RegistryFile":
        """Docs/06 §5b: an obligation registry's ``connector`` MUST name a
        declared connector — an undeclared adapter is a load error, not a
        runtime surprise."""
        for name, decl in self.obligationRegistries.items():
            if decl.connector not in self.connectors:
                raise ValueError(
                    f"obligation registry {name!r} names undeclared connector "
                    f"{decl.connector!r} (docs/06 §5b)"
                )
        return self

    @model_validator(mode="before")
    @classmethod
    def _split_precondition_decls(cls, data: Any) -> Any:
        """Normalise ``preconditionChecks`` items (bare name | object, v0.6
        CS-029) into the name tuple plus a ``precondition_decls`` map. Every
        check gets a declaration; bare names get the two-valued default."""
        if not isinstance(data, dict):
            return data
        raw = data.get("preconditionChecks")
        if raw is None:
            return data
        names: list[str] = []
        decls = dict(data.get("precondition_decls") or {})
        for item in raw:
            if isinstance(item, Mapping):
                name = str(item.get("name", ""))
                names.append(name)
                decls.setdefault(name, dict(item))
            else:
                names.append(str(item))
                decls.setdefault(str(item), {"name": str(item)})
        return {**data, "preconditionChecks": tuple(names), "precondition_decls": decls}


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
        connector_name = action.connector or resource.connector
        return ResolvedAction(
            kind=action.kind,
            resource=call.resource,
            action=call.action,
            data=dict(call.data),
            attrs=action.attributes(),
            connector=connector_name,
            connector_digest=self._data.connector_digests.get(connector_name),
            from_states=action.from_states,
            compensation=action.compensation,
        )

    # --- registry introspection used by the linter (M1) and gates (M2) ---

    @property
    def connector_digests(self) -> Mapping[str, str]:
        """The declared connector→digest pins (CS-020); empty when none are pinned."""
        return self._data.connector_digests

    def connector_digest(self, name: str) -> str | None:
        """The pinned artifact digest for a connector, or ``None`` if unpinned."""
        return self._data.connector_digests.get(name)

    def has_scope_predicate(self, name: str) -> bool:
        return name in self._data.scopePredicates

    def has_content_hook(self, name: str) -> bool:
        return name in self._data.contentHooks

    def has_precondition_check(self, name: str) -> bool:
        return name in self._data.preconditionChecks

    def precondition_decl(self, name: str) -> PreconditionCheckDecl | None:
        """The full declaration behind a check name (v0.6 CS-029); ``None`` for
        an undeclared name."""
        return self._data.precondition_decls.get(name)

    def check_hold_capable(self, name: str) -> bool:
        """Whether the check declared ``holdCapable`` (RFC §7.6 rule 3, CS-026).
        A hold from a check that didn't is an implementation error — the gate
        resolves it fail-closed."""
        decl = self._data.precondition_decls.get(name)
        return decl is not None and decl.holdCapable

    def reason_class(self, check: str, code: str) -> RetryClass:
        """The declared retry class of one check code (CS-029); undeclared
        codes default ``terminal`` — the safe direction is to stop retrying."""
        decl = self._data.precondition_decls.get(check)
        if decl is None:
            return RetryClass.TERMINAL
        return decl.reasonCodes.get(code, RetryClass.TERMINAL)

    def has_obligation_registry(self, name: str) -> bool:
        return name in self._data.obligationRegistries

    def obligation_registry(self, name: str) -> ObligationRegistryDecl | None:
        """The declaration behind an obligation registry name (v0.6 CS-034);
        ``None`` for an undeclared name — the ``requireMatch`` gate treats that
        as fail-closed and §13 rule 14 rejects it at load."""
        return self._data.obligationRegistries.get(name)

    @property
    def obligation_registries(self) -> Mapping[str, ObligationRegistryDecl]:
        return self._data.obligationRegistries

    def has_named_set(self, name: str) -> bool:
        return name in self._data.sets

    def named_set(self, name: str) -> tuple[str, ...]:
        return self._data.sets.get(name, ())

    def has_sink(self, name: str) -> bool:
        return name in self._data.sinks

    def classification_rank(self, label: str) -> int | None:
        """The label's position in the declared classification order (CS-024),
        lowest first — or ``None`` for a label not in the order, which the
        ``disclosure`` gate treats as fail-closed (RFC §8 runtime resolution)."""
        try:
            return self._data.classifications.index(label)
        except ValueError:
            return None

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

    Kept I/O-free so ``stonefold_core`` stays pure; callers read the file.
    """

    return InMemoryRegistry(RegistryFile.model_validate(data))
