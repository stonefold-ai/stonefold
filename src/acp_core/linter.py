"""The semantic linter (RFC §13) — all nine validation checks.

A policy that produces any ERROR-severity finding MUST NOT load: the gateway
refuses to start rather than fall back to a permissive default (design §4 review
note — a silently-degraded control plane is how a gateway fails open by
accident). WARN findings are reported but do not block loading.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from acp_core.condition import parse_and_validate
from acp_core.enums import Kind, Reversibility
from acp_core.policy import FailureMode, Policy, Targets
from acp_core.registry import ActionDef, InMemoryRegistry

_SENSITIVE_FLOOR = frozenset({"public", "internal"})


class Severity(str, Enum):
    ERROR = "error"
    WARN = "warn"


@dataclass(frozen=True)
class LintFinding:
    code: str  # e.g. "13.5"
    severity: Severity
    message: str


@dataclass
class LintReport:
    findings: list[LintFinding] = field(default_factory=list)

    def add(self, code: str, severity: Severity, message: str) -> None:
        self.findings.append(LintFinding(code, severity, message))

    @property
    def errors(self) -> list[LintFinding]:
        return [f for f in self.findings if f.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[LintFinding]:
        return [f for f in self.findings if f.severity is Severity.WARN]

    @property
    def has_errors(self) -> bool:
        return any(f.severity is Severity.ERROR for f in self.findings)

    def format(self) -> str:
        return "\n".join(
            f"  [{f.severity.value.upper()} §{f.code}] {f.message}"
            for f in self.findings
        )


class PolicyError(Exception):
    """Raised when a policy has ERROR-severity lint findings (prevents load)."""

    def __init__(self, report: LintReport) -> None:
        self.report = report
        super().__init__(
            "policy failed validation:\n" + report.format()
        )


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _merged_gates(
    policy: Policy, resource: str, action: str | None, kind: Kind
) -> dict[str, Any]:
    keys: list[str] = []
    if action is not None:
        keys.append(f"{resource}.{action}")
        keys.append(action)
    keys.append(kind.value)
    keys.append("*")
    merged: dict[str, Any] = {}
    for key in keys:
        for gate_name, cfg in policy.gates.get(key, {}).items():
            merged.setdefault(gate_name, cfg)
    return merged


def _allowed_action_defs(
    policy: Policy, registry: InMemoryRegistry
) -> list[tuple[str, str, ActionDef]]:
    """Enumerate (resource, action, def) the policy *allows* (deny ignored —
    §13.4/§13.7/§13.8 only critique what is actually permitted)."""
    out: list[tuple[str, str, ActionDef]] = []
    resources = registry.file.resources
    for pmap in policy.allow:
        for kind, target in pmap.items():
            if target == "*":
                for rname, rdef in resources.items():
                    for aname, adef in rdef.actions.items():
                        if adef.kind == kind:
                            out.append((rname, aname, adef))
            elif isinstance(target, list):
                for token in target:
                    if token in resources:
                        for aname, adef in resources[token].actions.items():
                            if adef.kind == kind:
                                out.append((token, aname, adef))
                    else:
                        for rname, rdef in resources.items():
                            cand = rdef.actions.get(token)
                            if cand is not None and cand.kind == kind:
                                out.append((rname, token, cand))
            elif isinstance(target, dict):
                for rname, names in target.items():
                    rdef_opt = resources.get(rname)
                    if rdef_opt is None:
                        continue
                    for aname in names:
                        cand = rdef_opt.actions.get(aname)
                        if cand is not None:
                            out.append((rname, aname, cand))
    return out


def _all_action_names(registry: InMemoryRegistry) -> set[str]:
    names: set[str] = set()
    for rdef in registry.file.resources.values():
        names.update(rdef.actions)
    return names


# --------------------------------------------------------------------------
# the nine checks
# --------------------------------------------------------------------------
def lint(policy: Policy, registry: InMemoryRegistry) -> LintReport:
    report = LintReport()
    resources = registry.file.resources
    known_resources = set(resources)
    known_actions = _all_action_names(registry)

    _check_names_exist(policy, registry, known_resources, known_actions, report)
    _check_overlap(policy, report)
    _check_transition_from_states(policy, registry, report)
    _check_irreversible_unguarded(policy, registry, report)
    _check_open_on_irreversible(policy, registry, report)
    _check_star_grants(policy, report)
    _check_assess_explainability(policy, registry, report)
    _check_reads_disclosure(policy, registry, report)
    _check_conditions(policy, report)
    return report


def _iter_permission_targets(
    policy: Policy,
) -> list[tuple[Kind, Targets]]:
    pairs: list[tuple[Kind, Targets]] = []
    for pmap in (*policy.allow, *policy.deny):
        pairs.extend(pmap.items())
    for std in policy.standing:
        pairs.extend(std.enables.items())
    return pairs


def _check_names_exist(
    policy: Policy,
    registry: InMemoryRegistry,
    known_resources: set[str],
    known_actions: set[str],
    report: LintReport,
) -> None:
    """§13.1 — every resource/action/scope/hook name referenced exists."""
    for _kind, target in _iter_permission_targets(policy):
        if target == "*":
            continue
        if isinstance(target, list):
            for token in target:
                if token not in known_resources and token not in known_actions:
                    report.add(
                        "13.1",
                        Severity.ERROR,
                        f"unknown name {token!r} (not a registered resource or action)",
                    )
        elif isinstance(target, dict):
            for rname, names in target.items():
                rdef = registry.file.resources.get(rname)
                if rdef is None:
                    report.add("13.1", Severity.ERROR, f"unknown resource {rname!r}")
                    continue
                for aname in names:
                    if aname not in rdef.actions:
                        report.add(
                            "13.1",
                            Severity.ERROR,
                            f"unknown action {rname}.{aname!r}",
                        )

    # scope: resource keys + named predicate values. A predicate value may be a
    # bare name (`assignedToCurrentUser`) or a call form (`inWard(actor.ward)`)
    # — RFC §6.3. Only the *name* (before any `(`) is registered.
    for rname, pred in policy.scope.items():
        if rname not in known_resources:
            report.add("13.1", Severity.ERROR, f"scope on unknown resource {rname!r}")
        pred_name = pred.split("(", 1)[0].strip()
        if not registry.has_scope_predicate(pred_name):
            report.add(
                "13.1", Severity.ERROR, f"unknown scope predicate {pred_name!r}"
            )

    # gate keys + gate-config name references
    for key, gateset in policy.gates.items():
        _check_gate_key(key, registry, known_resources, known_actions, report)
        _check_gate_config_names(key, gateset, registry, report)


def _check_gate_key(
    key: str,
    registry: InMemoryRegistry,
    known_resources: set[str],
    known_actions: set[str],
    report: LintReport,
) -> None:
    if key == "*" or key in {k.value for k in Kind}:
        return
    if "." in key:
        rname, _, aname = key.partition(".")
        rdef = registry.file.resources.get(rname)
        if rdef is None or aname not in rdef.actions:
            report.add("13.1", Severity.ERROR, f"gate on unknown action {key!r}")
        return
    if key not in known_actions:
        report.add("13.1", Severity.ERROR, f"gate on unknown action {key!r}")


def _check_gate_config_names(
    key: str,
    gateset: dict[str, Any],
    registry: InMemoryRegistry,
    report: LintReport,
) -> None:
    for gate_name, cfg in gateset.items():
        if gate_name in ("allowlist", "denylist") and isinstance(cfg, dict):
            set_name = cfg.get("set")
            if set_name is not None and not registry.has_named_set(set_name):
                report.add("13.1", Severity.ERROR, f"unknown named set {set_name!r} in {key}.{gate_name}")
        elif gate_name == "contentCheck":
            for hook in [cfg] if isinstance(cfg, str) else cfg:
                if not registry.has_content_hook(hook):
                    report.add("13.1", Severity.ERROR, f"unknown content hook {hook!r} in {key}")
        elif gate_name in ("precondition", "emissionControl") and isinstance(cfg, dict):
            for chk in cfg.get("checks", []):
                if not registry.has_precondition_check(chk):
                    report.add("13.1", Severity.ERROR, f"unknown precondition check {chk!r} in {key}")
        elif gate_name in ("precondition", "emissionControl") and isinstance(cfg, list):
            for chk in cfg:
                if not registry.has_precondition_check(chk):
                    report.add("13.1", Severity.ERROR, f"unknown precondition check {chk!r} in {key}")
        elif gate_name == "disclosure" and isinstance(cfg, dict):
            for sink in cfg.get("allowSink", []):
                if not registry.has_sink(sink):
                    report.add("13.1", Severity.ERROR, f"unknown sink {sink!r} in {key}")


def _check_overlap(policy: Policy, report: LintReport) -> None:
    """§13.2 — overlapping allow/deny intent SHOULD warn (deny still wins)."""
    allow_tokens: dict[Kind, set[str]] = {}
    deny_tokens: dict[Kind, set[str]] = {}
    for pmap in policy.allow:
        for kind, target in pmap.items():
            if isinstance(target, list):
                allow_tokens.setdefault(kind, set()).update(target)
    for pmap in policy.deny:
        for kind, target in pmap.items():
            if isinstance(target, list):
                deny_tokens.setdefault(kind, set()).update(target)
    for kind in allow_tokens.keys() & deny_tokens.keys():
        for token in allow_tokens[kind] & deny_tokens[kind]:
            report.add(
                "13.2",
                Severity.WARN,
                f"{kind.value} {token!r} appears in both allow and deny (deny wins)",
            )


def _check_transition_from_states(
    policy: Policy, registry: InMemoryRegistry, report: LintReport
) -> None:
    """§13.3 — every referenced transition action has declared from-states."""
    for _kind, target in _iter_permission_targets(policy):
        if isinstance(target, dict):
            for rname, names in target.items():
                rdef = registry.file.resources.get(rname)
                if rdef is None:
                    continue
                for aname in names:
                    adef = rdef.actions.get(aname)
                    if adef and adef.kind is Kind.TRANSITION and not adef.from_states:
                        report.add(
                            "13.3",
                            Severity.ERROR,
                            f"transition {rname}.{aname} has no declared from-states",
                        )
    # also transition gate keys
    for key in policy.gates:
        if "." in key:
            rname, _, aname = key.partition(".")
            rdef = registry.file.resources.get(rname)
            if rdef is None:
                continue
            adef = rdef.actions.get(aname)
            if adef and adef.kind is Kind.TRANSITION and not adef.from_states:
                report.add(
                    "13.3",
                    Severity.ERROR,
                    f"transition {key} has no declared from-states",
                )


def _check_irreversible_unguarded(
    policy: Policy, registry: InMemoryRegistry, report: LintReport
) -> None:
    """§13.4 — irreversible allowed action with no approval/dual-auth/precondition."""
    guards = {"requireApproval", "dualAuthorization", "precondition"}
    for rname, aname, adef in _allowed_action_defs(policy, registry):
        if adef.reversibility is not Reversibility.IRREVERSIBLE:
            continue
        gates = _merged_gates(policy, rname, aname, adef.kind)
        if not (guards & gates.keys()):
            report.add(
                "13.4",
                Severity.WARN,
                f"irreversible action {rname}.{aname} has no requireApproval/"
                f"dualAuthorization/precondition gate",
            )


def _check_open_on_irreversible(
    policy: Policy, registry: InMemoryRegistry, report: LintReport
) -> None:
    """§13.5 — failureMode: open on an irreversible action ⇒ ERROR.

    ACP-AMBIGUITY: the RFC allows "explicit acknowledgement" to downgrade this;
    the schema has no ack field, so we always ERROR (the safer reading)."""
    if policy.defaults.failureMode is not FailureMode.OPEN:
        return
    irreversibles = [
        f"{r}.{a}"
        for r, a, d in _allowed_action_defs(policy, registry)
        if d.reversibility is Reversibility.IRREVERSIBLE
    ]
    for name in sorted(set(irreversibles)):
        report.add(
            "13.5",
            Severity.ERROR,
            f"failureMode: open with irreversible action {name} (not acknowledged)",
        )


def _check_star_grants(policy: Policy, report: LintReport) -> None:
    """§13.6 — '*' grants ⇒ WARN."""
    for pmap in (*policy.allow, *policy.deny):
        for kind, target in pmap.items():
            if target == "*":
                report.add(
                    "13.6",
                    Severity.WARN,
                    f"'*' grant on kind {kind.value} (prefer explicit enumeration)",
                )


def _check_assess_explainability(
    policy: Policy, registry: InMemoryRegistry, report: LintReport
) -> None:
    """§13.7 — assess + explainability: required but no requireExplanation ⇒ ERROR."""
    for rname, aname, adef in _allowed_action_defs(policy, registry):
        if adef.kind is not Kind.ASSESS:
            continue
        if adef.explainability.value != "required":
            continue
        gates = _merged_gates(policy, rname, aname, adef.kind)
        if not gates.get("requireExplanation"):
            report.add(
                "13.7",
                Severity.ERROR,
                f"assess {rname}.{aname} requires explainability but has no "
                f"requireExplanation gate",
            )


def _check_reads_disclosure(
    policy: Policy, registry: InMemoryRegistry, report: LintReport
) -> None:
    """§13.8 — observe of resultSensitivity > internal with no disclosure ⇒ WARN."""
    for rname, aname, adef in _allowed_action_defs(policy, registry):
        if adef.kind is not Kind.OBSERVE:
            continue
        if adef.resultSensitivity in _SENSITIVE_FLOOR:
            continue
        gates = _merged_gates(policy, rname, aname, adef.kind)
        if "disclosure" not in gates:
            report.add(
                "13.8",
                Severity.WARN,
                f"observe {rname}.{aname} returns {adef.resultSensitivity!r} "
                f"but has no disclosure gate",
            )


def _check_conditions(policy: Policy, report: LintReport) -> None:
    """§13.9 — every condition parses and references known namespaces/functions."""
    for std in policy.standing:
        for problem in parse_and_validate(std.when):
            report.add("13.9", Severity.ERROR, f"standing[{std.name}]: {problem}")
    for key, gateset in policy.gates.items():
        for gate_name, cfg in gateset.items():
            if isinstance(cfg, dict) and isinstance(cfg.get("when"), str):
                for problem in parse_and_validate(cfg["when"]):
                    report.add("13.9", Severity.ERROR, f"gates[{key}].{gate_name}: {problem}")
