# SPDX-License-Identifier: Apache-2.0
"""Questions answerable about a policy without running it.

A reviewer, an auditor and a regulatory assessment all **read** a policy; none of
them runs the system, because the entire value of a review is knowing before the
incident rather than after it. So the useful test of a policy language is not only
what it can enforce — it is what can be *proved over it while it sits still*.

This module answers the one question that motivated the ``reads:`` declaration:

    the critical-analyte list is 25 days overdue.
    Which gates are affected, and what do they do about it?

And the half that matters more, which no amount of enforcement provides: **which
comparable gates read nothing at all**. A gate whose author forgot the freshness
check is indistinguishable from a correct one at runtime — it returns confident
answers computed from last year's content. It is visible only next to its
neighbours that did declare the dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from stonefold_core.compiler import CompiledPolicy
from stonefold_core.policy import Policy
from stonefold_core.registry import InMemoryRegistry


#: Gates that can depend on content living outside the policy — a registered
#: check, a content hook, an obligation registry. Everything else compares the
#: intent against constants the policy itself carries (a `valueLimit`'s ceiling, a
#: `rate`'s window), and a constant cannot go stale, so listing those as "reads
#: nothing" would bury the real finding in noise. This is the set the `silent`
#: list is drawn from.
CONTENT_DEPENDENT_GATES = frozenset({
    "precondition", "emissionControl", "contentCheck", "requireMatch",
})


@dataclass(frozen=True)
class GateRead:
    """One gate's declared dependency on one source."""

    resource: str
    action: str
    gate: str
    source: str
    freshness: str = ""
    on_stale: str = "hold"
    on_unavailable: str = "deny"


@dataclass(frozen=True)
class ReadsReport:
    """What a policy says about one source, and what it does not say.

    ``silent`` is the finding: **content-dependent** gates that declare no source
    at all. Restricted to the gates that *could* read something outside the policy
    (a registered check, a content hook, an obligation registry), because a
    `valueLimit` compares against a number written in the policy and a number
    cannot go stale — listing those would bury the real finding in noise.

    A gate in this list is not necessarily wrong; plenty of checks depend on
    nothing that ages. But a gate that *should* have read the overdue rule set is
    in this list and nowhere else.
    """

    source: str
    declared: bool
    readers: tuple[GateRead, ...]
    silent: tuple[tuple[str, str, str], ...]

    def summary(self) -> str:
        """The answer a reviewer wants, in the shape they asked the question."""
        if not self.declared:
            return f"{self.source!r} is not declared in the registry"
        lines = [f"{self.source} is read by {len(self.readers)} gate(s):"]
        for r in self.readers:
            detail = f"freshness {r.freshness or '—'}  onStale {r.on_stale}  onUnavailable {r.on_unavailable}"
            lines.append(f"  {r.resource}.{r.action}  {r.gate}  {detail}")
        if self.silent:
            lines.append(
                f"{len(self.silent)} gate(s) of the same kind read NO declared source:"
            )
            for resource, action, gate in self.silent:
                lines.append(f"  {resource}.{action}  {gate}  <- reads nothing")
        return "\n".join(lines)


def _gate_map(policy: Policy, registry: InMemoryRegistry) -> list[tuple[str, str, str, Any]]:
    """Every (resource, action, gate name, gate config) the policy declares.

    Reads the policy's own gate tables rather than the compiled matcher, because
    the question is about what the *document* says.
    """
    rows: list[tuple[str, str, str, Any]] = []
    for resource, rdef in registry.file.resources.items():
        for action in rdef.actions:
            for scope in (policy.gates.get(action), policy.gates.get(f"{resource}.{action}")):
                if not isinstance(scope, dict):
                    continue
                for gate, cfg in scope.items():
                    rows.append((resource, action, gate, cfg))
    return rows


def gates_reading(
    policy: Policy | CompiledPolicy, registry: InMemoryRegistry, source: str
) -> ReadsReport:
    """Which gates declare that they read ``source`` — and which read nothing.

    Static: no adapters, no clock, no I/O. It answers from the policy document and
    the registry alone, which is the only form of answer a safety case can use.
    """
    doc = policy.policy if isinstance(policy, CompiledPolicy) else policy
    rows = _gate_map(doc, registry)

    readers: list[GateRead] = []
    silent: list[tuple[str, str, str]] = []
    for resource, action, gate, cfg in rows:
        reads = cfg.get("reads") if isinstance(cfg, dict) else None
        named = _sources_named(reads)
        if source in named:
            readers.append(GateRead(
                resource=resource, action=action, gate=gate, source=source,
                freshness=_freshness_of(reads, source),
                on_stale=_on_stale_of(reads, source),
                on_unavailable=str(cfg.get("onUnavailable", "deny")),
            ))
        elif not named and gate in CONTENT_DEPENDENT_GATES:
            silent.append((resource, action, gate))

    return ReadsReport(
        source=source,
        declared=source in registry.file.sources,
        readers=tuple(readers),
        silent=tuple(silent),
    )


def declared_sources_unread(
    policy: Policy | CompiledPolicy, registry: InMemoryRegistry
) -> tuple[str, ...]:
    """Sources the registry declares that no gate reads.

    Usually a leftover, occasionally the more interesting case: somebody declared
    the dependency, meaning they knew it existed, and no control uses it.
    """
    doc = policy.policy if isinstance(policy, CompiledPolicy) else policy
    read: set[str] = set()
    for _resource, _action, _gate, cfg in _gate_map(doc, registry):
        if isinstance(cfg, dict):
            read |= _sources_named(cfg.get("reads"))
    return tuple(sorted(set(registry.file.sources) - read))


def _sources_named(reads: Any) -> set[str]:
    if not isinstance(reads, list):
        return set()
    out: set[str] = set()
    for entry in reads:
        if isinstance(entry, str):
            out.add(entry)
        elif isinstance(entry, dict) and entry.get("source"):
            out.add(str(entry["source"]))
    return out


def _entry_for(reads: Any, source: str) -> dict[str, Any] | None:
    if not isinstance(reads, list):
        return None
    for entry in reads:
        if isinstance(entry, dict) and str(entry.get("source", "")) == source:
            return entry
    return None


def _freshness_of(reads: Any, source: str) -> str:
    entry = _entry_for(reads, source)
    return str(entry.get("freshness", "")) if entry else ""


def _on_stale_of(reads: Any, source: str) -> str:
    entry = _entry_for(reads, source)
    return str(entry.get("onStale", "hold")) if entry else "hold"
