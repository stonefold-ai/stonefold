"""Policy compilation: allow/deny → an indexed matcher (design §4, RFC §6.2).

At load the policy is compiled once into ``KindMatcher`` structures so that
matching an attempted action is a handful of set lookups, not a YAML re-parse.
``CompiledPolicy`` is what the pipeline holds and queries per request:

* ``authorize`` — default deny → deny-wins → most-specific allow (RFC §6.2).
* ``gate_keys_for`` — which gate configs apply (action + kind + '*', AND'd).
* ``scope_for`` — the named scope predicate for a resource (RFC §6.3).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import TYPE_CHECKING, Any

from stonefold_core.enums import Kind
from stonefold_core.models import ResolvedAction
from stonefold_core.policy import Policy, Targets

if TYPE_CHECKING:
    from stonefold_core.linter import LintReport


class MatchSpecificity(IntEnum):
    """How specifically an allow rule matched (RFC §6.2 rule 4): a named action
    beats a bare resource, which beats a kind-level ``*``."""

    STAR = 1
    RESOURCE = 2
    ACTION = 3


@dataclass
class _KindIndex:
    star: bool = False
    tokens: set[str] = field(default_factory=set)  # bare resources or actions
    action_map: dict[str, set[str]] = field(default_factory=dict)  # {Resource: {actions}}


class KindMatcher:
    """A compiled view of one of allow/deny, indexed by kind."""

    def __init__(self) -> None:
        self._by_kind: dict[Kind, _KindIndex] = {}

    def add(self, kind: Kind, target: Targets) -> None:
        idx = self._by_kind.setdefault(kind, _KindIndex())
        if target == "*":
            idx.star = True
        elif isinstance(target, list):
            idx.tokens.update(target)
        elif isinstance(target, dict):
            for resource, actions in target.items():
                idx.action_map.setdefault(resource, set()).update(actions)

    def match(self, kind: Kind, resource: str, action: str | None) -> MatchSpecificity | None:
        idx = self._by_kind.get(kind)
        if idx is None:
            return None
        best: MatchSpecificity | None = None
        if idx.star:
            best = MatchSpecificity.STAR
        # A bare token may name the resource (grants all actions of the kind) or
        # the specific action; we never need to pre-classify it — we test the
        # resolved action's own resource/name against the token set.
        if action is not None and action in idx.tokens:
            best = _max_spec(best, MatchSpecificity.ACTION)
        if resource in idx.tokens:
            best = _max_spec(best, MatchSpecificity.RESOURCE)
        if action is not None:
            named = idx.action_map.get(resource)
            if named is not None and action in named:
                best = _max_spec(best, MatchSpecificity.ACTION)
        return best


def _max_spec(
    a: MatchSpecificity | None, b: MatchSpecificity
) -> MatchSpecificity:
    return b if a is None else max(a, b)


@dataclass(frozen=True)
class AuthzResult:
    """The authorization-stage verdict (before scope/gates)."""

    allowed: bool
    rule: str  # "deny-rule" | "default-deny" | "allow"
    specificity: MatchSpecificity | None = None


class CompiledPolicy:
    """Compiled, ready-to-query policy held by the gateway at runtime."""

    def __init__(self, policy: Policy) -> None:
        self.policy = policy
        self.lint_report: "LintReport | None" = None
        self._allow = KindMatcher()
        self._deny = KindMatcher()
        for pmap in policy.allow:
            for kind, target in pmap.items():
                self._allow.add(kind, target)
        for pmap in policy.deny:
            for kind, target in pmap.items():
                self._deny.add(kind, target)

    @property
    def agent(self) -> str:
        return self.policy.agent

    def authorize(self, a: ResolvedAction) -> AuthzResult:
        """RFC §6.2: 1) default deny, 2) deny wins, 3) most-specific allow.

        NOTE on ``standing`` (RFC §7.15): standing grants are conditional allows
        evaluated against context (M2). They are NOT applied here, so an action
        in both ``deny`` and ``standing.enables`` stays denied — deny always
        wins (RFC §6.2). RFC v0.3 (CS-010) made this explicit: a standing-only
        action is left out of both ``allow`` and ``deny`` (default-deny covers
        the off state), and the linter rejects the deny∩standing combination
        as unsatisfiable (§13 rule 11).
        """
        if self._deny.match(a.kind, a.resource, a.action) is not None:
            return AuthzResult(allowed=False, rule="deny-rule")
        spec = self._allow.match(a.kind, a.resource, a.action)
        if spec is not None:
            return AuthzResult(allowed=True, rule="allow", specificity=spec)
        return AuthzResult(allowed=False, rule="default-deny")

    def gate_keys_for(self, a: ResolvedAction) -> list[str]:
        """Gate keys that apply to ``a``, most-specific first (RFC §7: all
        matching gates are AND-combined). Keys may be ``Resource.action``, a
        bare action name, a kind, or ``*``."""
        candidates: list[str] = []
        if a.action is not None:
            candidates.append(f"{a.resource}.{a.action}")
            candidates.append(a.action)
        candidates.append(a.kind.value)
        candidates.append("*")
        seen: set[str] = set()
        keys: list[str] = []
        for key in candidates:
            if key in self.policy.gates and key not in seen:
                seen.add(key)
                keys.append(key)
        return keys

    def gates_for(self, a: ResolvedAction) -> dict[str, dict[str, Any]]:
        """The merged gate configs applying to ``a`` (M2 consumes this)."""
        merged: dict[str, dict[str, Any]] = {}
        for key in self.gate_keys_for(a):
            for gate_name, cfg in self.policy.gates[key].items():
                # Most-specific key wins on conflict (RFC §3.2 spirit: more
                # restrictive / more specific governs).
                merged.setdefault(gate_name, cfg)
        return merged

    def scope_for(self, resource: str) -> str | None:
        return self.policy.scope.get(resource)
