"""The semantic linter (RFC §13) — the rule-1..18 validation checks.

A policy that produces any ERROR-severity finding MUST NOT load: the gateway
refuses to start rather than fall back to a permissive default (design §4 review
note — a silently-degraded control plane is how a gateway fails open by
accident). WARN findings are reported but do not block loading; INFO findings
(v0.6, rule 15's deployment-check pointer) are purely advisory.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from stonefold_core.condition import Path, parse, parse_and_validate
from stonefold_core.enums import Kind, Reversibility
from stonefold_core.policy import FailureMode, Policy, Targets
from stonefold_core.registry import ActionDef, InMemoryRegistry

_SENSITIVE_FLOOR = frozenset({"public", "internal"})

# The extra read-only namespace legal ONLY inside requireMatch match/provenance
# clauses (RFC §8 note, v0.6 CS-036).
_OBLIGATION_NS = frozenset({"obligation"})


class Severity(str, Enum):
    ERROR = "error"
    WARN = "warn"
    INFO = "info"


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
    def infos(self) -> list[LintFinding]:
        return [f for f in self.findings if f.severity is Severity.INFO]

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
# the checks
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
    _check_compensable_has_compensation(policy, registry, report)
    _check_standing_deny_conflict(policy, report)
    _check_ambiguous_bare_allow(policy, registry, report)
    _check_dual_auth_quorum(policy, report)
    _check_hold_capable_resolvers(policy, registry, report)
    _check_require_match(policy, registry, report)
    _check_creation_execution_separation(policy, registry, report)
    _check_consume_none_irreversible(policy, registry, report)
    return report


def _check_hold_capable_resolvers(
    policy: Policy, registry: InMemoryRegistry, report: LintReport
) -> None:
    """§13.18 (v0.6, CS-038) — a hold-capable check gated with no ``resolvers:``
    ⇒ warn: its holds release under the deployment's default resolver role, and
    are refused ``hold-unresolvable`` if none is configured. (The other half of
    rule 18 — ``holdCapable`` without ``reasonCodes`` — is a registry load
    error, ``PreconditionCheckDecl``.)"""
    for key, gateset in policy.gates.items():
        for gate_name in ("precondition", "emissionControl"):
            cfg = gateset.get(gate_name)
            if cfg is None:
                continue
            if isinstance(cfg, dict):
                if cfg.get("resolvers"):
                    continue
                checks = list(cfg.get("checks") or [])
            elif isinstance(cfg, list):
                checks = list(cfg)
            else:
                checks = [cfg]
            for chk in checks:
                if registry.check_hold_capable(str(chk)):
                    report.add(
                        "13.18",
                        Severity.WARN,
                        f"hold-capable check {chk!r} in {key}.{gate_name} declares no "
                        "resolvers: — its holds fall to the deployment default resolver "
                        "role, and are refused hold-unresolvable if none is configured",
                    )


def _clause_obligation_paths(clause: str) -> list[str]:
    """The record-relative ``obligation.*`` paths a parsed clause references
    (empty for an unparsable clause — rule 14's parse check reports that)."""
    try:
        expr = parse(clause)
    except Exception:
        return []
    out: list[str] = []
    stack: list[Any] = [expr]
    while stack:
        node = stack.pop()
        if isinstance(node, Path):
            if len(node.parts) > 1 and node.parts[0] == "obligation":
                out.append(".".join(node.parts[1:]))
        elif hasattr(node, "left") and hasattr(node, "right"):  # And/Or/Compare/In
            stack.append(node.left)
            stack.append(node.right)
        elif hasattr(node, "args"):  # Func
            stack.extend(node.args)
        elif hasattr(node, "expr"):  # Not
            stack.append(node.expr)
        elif hasattr(node, "path"):  # Exists
            stack.append(node.path)
    return out


def _check_require_match(
    policy: Policy, registry: InMemoryRegistry, report: LintReport
) -> None:
    """§13.14 + §13.17 (v0.6, CS-038) — ``requireMatch`` typing: the registry
    is declared; every ``obligation.*`` path in ``match``/``provenance``/
    ``consume`` exists in its declared schema; a tolerance clause applies to a
    numeric/money field; clause strings parse under the ``obligation``
    namespace; ``onAmbiguous: allow`` is illegal."""
    for key, gateset in policy.gates.items():
        cfg = gateset.get("requireMatch")
        if cfg is None:
            continue
        where = f"gates[{key}].requireMatch"
        if not isinstance(cfg, dict):
            report.add("13.14", Severity.ERROR, f"{where}: must be a mapping")
            continue
        if cfg.get("onAmbiguous") == "allow":
            report.add(
                "13.17",
                Severity.ERROR,
                f"{where}: onAmbiguous: allow is illegal — the gateway never "
                f"auto-selects among candidate obligations (§7.16)",
            )
        reg_name = str(cfg.get("registry") or "")
        decl = registry.obligation_registry(reg_name)
        if decl is None:
            report.add(
                "13.14",
                Severity.ERROR,
                f"{where}: unknown obligation registry {reg_name!r}",
            )
        for section in ("match", "provenance"):
            for clause in cfg.get(section) or []:
                if isinstance(clause, str):
                    for problem in parse_and_validate(
                        clause, extra_namespaces=_OBLIGATION_NS
                    ):
                        report.add("13.14", Severity.ERROR, f"{where}.{section}: {problem}")
                    if decl is not None:
                        for path in _clause_obligation_paths(clause):
                            if not decl.has_path(path):
                                report.add(
                                    "13.14",
                                    Severity.ERROR,
                                    f"{where}.{section}: obligation.{path} is not "
                                    f"in {reg_name!r}'s declared schema",
                                )
                elif isinstance(clause, dict):
                    _check_tolerance_clause(clause, decl, reg_name, f"{where}.{section}", report)
                else:
                    report.add(
                        "13.14", Severity.ERROR, f"{where}.{section}: bad clause {clause!r}"
                    )
        consume = cfg.get("consume")
        if isinstance(consume, str) and consume != "none" and consume.startswith("obligation."):
            if decl is not None and not decl.has_path(consume[len("obligation.") :]):
                report.add(
                    "13.14",
                    Severity.ERROR,
                    f"{where}: consume path {consume} is not in {reg_name!r}'s declared schema",
                )
        elif consume != "none":
            report.add(
                "13.14",
                Severity.ERROR,
                f"{where}: consume must be an obligation.* path or 'none', got {consume!r}",
            )


def _check_tolerance_clause(
    clause: dict[str, Any],
    decl: Any,
    reg_name: str,
    where: str,
    report: LintReport,
) -> None:
    field_path = clause.get("field")
    if not (isinstance(field_path, str) and field_path.startswith("obligation.")):
        report.add(
            "13.14", Severity.ERROR, f"{where}: tolerance field must be obligation.*"
        )
        return
    rel = field_path[len("obligation.") :]
    if decl is not None:
        if not decl.has_path(rel):
            report.add(
                "13.14",
                Severity.ERROR,
                f"{where}: {field_path} is not in {reg_name!r}'s declared schema",
            )
        elif not decl.is_numeric(rel):
            report.add(
                "13.14",
                Severity.ERROR,
                f"{where}: tolerance (within) on {field_path}, which is not a "
                f"declared numeric/money field (§7.16)",
            )


def _matched_registry_connectors(
    policy: Policy, registry: InMemoryRegistry
) -> set[str]:
    """Connectors backing every obligation registry this policy matches
    against — the statically-visible link rule 15 checks."""
    out: set[str] = set()
    for gateset in policy.gates.values():
        cfg = gateset.get("requireMatch")
        if isinstance(cfg, dict):
            decl = registry.obligation_registry(str(cfg.get("registry") or ""))
            if decl is not None:
                out.add(decl.connector)
    return out


def _check_creation_execution_separation(
    policy: Policy, registry: InMemoryRegistry, report: LintReport
) -> None:
    """§13.15 (v0.6, CS-038) — the governed agent must not author its own
    obligations. ERROR where the overlap is statically visible: the policy
    allows a ``record``/``effect``/``transition`` on a resource backed by the
    same connector as an obligation registry it matches against. Otherwise
    (the registry is external) an INFO points at the deployment check
    (docs/06 §5b: the agent's principal must not hold write access)."""
    matched = _matched_registry_connectors(policy, registry)
    if not matched:
        return
    overlap = False
    for rname, aname, adef in _allowed_action_defs(policy, registry):
        if adef.kind not in (Kind.RECORD, Kind.EFFECT, Kind.TRANSITION):
            continue
        rdef = registry.file.resources.get(rname)
        connector = adef.connector or (rdef.connector if rdef else "")
        if connector in matched:
            overlap = True
            report.add(
                "13.15",
                Severity.ERROR,
                f"{adef.kind.value} {rname}.{aname} writes through connector "
                f"{connector!r}, which backs an obligation registry this policy "
                f"matches against — the agent must not author its own "
                f"obligations (§7.16, docs/06 §5b)",
            )
    if not overlap:
        report.add(
            "13.15",
            Severity.INFO,
            "creation/execution separation is not statically visible for the "
            "matched obligation registries — verify at deployment that the "
            "agent's principal holds no write access to them (docs/06 §5b)",
        )


def _check_consume_none_irreversible(
    policy: Policy, registry: InMemoryRegistry, report: LintReport
) -> None:
    """§13.16 (v0.6, CS-038) — ``consume: none`` on an irreversible effect ⇒
    WARN: verification without consumption leaves the double-spend window open
    in the decide→dispatch gap."""
    for rname, aname, adef in _allowed_action_defs(policy, registry):
        if adef.kind is not Kind.EFFECT:
            continue
        if adef.reversibility is not Reversibility.IRREVERSIBLE:
            continue
        cfg = _merged_gates(policy, rname, aname, adef.kind).get("requireMatch")
        if isinstance(cfg, dict) and cfg.get("consume") == "none":
            report.add(
                "13.16",
                Severity.WARN,
                f"irreversible effect {rname}.{aname} uses requireMatch with "
                f"consume: none — verification without consumption leaves the "
                f"double-spend window open (§7.16)",
            )


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
    """§13.4 — irreversible allowed action with no approval/dual-auth/
    precondition/requireMatch (rule 4 as amended by v0.6 CS-038: a matched
    obligation is a satisfying guard)."""
    guards = {"requireApproval", "dualAuthorization", "precondition", "requireMatch"}
    for rname, aname, adef in _allowed_action_defs(policy, registry):
        if adef.reversibility is not Reversibility.IRREVERSIBLE:
            continue
        gates = _merged_gates(policy, rname, aname, adef.kind)
        if not (guards & gates.keys()):
            report.add(
                "13.4",
                Severity.WARN,
                f"irreversible action {rname}.{aname} has no requireApproval/"
                f"dualAuthorization/precondition/requireMatch gate",
            )


def _check_open_on_irreversible(
    policy: Policy, registry: InMemoryRegistry, report: LintReport
) -> None:
    """§13.5 — failureMode: open on an irreversible action ⇒ ERROR.

    STONEFOLD-AMBIGUITY: the RFC allows "explicit acknowledgement" to downgrade this;
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


def _check_compensable_has_compensation(
    policy: Policy, registry: InMemoryRegistry, report: LintReport
) -> None:
    """§13.10 (CS-008) — a ``compensable`` allowed action MUST declare a
    compensation, and any declared compensation MUST name a resource+action that
    exists in the registry. Enforces the §5 definition of ``compensable`` ("a
    declared undo exists"); ``irreversible`` MAY declare one but is not required to.
    """
    resources = registry.file.resources
    for rname, aname, adef in _allowed_action_defs(policy, registry):
        comp = adef.compensation
        if adef.reversibility is Reversibility.COMPENSABLE and comp is None:
            report.add(
                "13.10",
                Severity.ERROR,
                f"compensable action {rname}.{aname} declares no compensation "
                f"(§5: compensable means a declared undo exists)",
            )
        if comp is not None:
            target = resources.get(comp.resource)
            if target is None or comp.action not in target.actions:
                report.add(
                    "13.10",
                    Severity.ERROR,
                    f"{rname}.{aname} declares compensation "
                    f"{comp.resource}.{comp.action}, which is not in the registry",
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


def _check_standing_deny_conflict(policy: Policy, report: LintReport) -> None:
    """§13.11 (v0.3, CS-010) — an action in both ``deny`` and a ``standing``
    rule's ``enables`` is unsatisfiable (deny always wins, §6.2) ⇒ ERROR."""
    deny_star: set[Kind] = set()
    deny_tokens: dict[Kind, set[str]] = {}
    deny_named: dict[Kind, set[tuple[str, str]]] = {}
    for pmap in policy.deny:
        for kind, target in pmap.items():
            if target == "*":
                deny_star.add(kind)
            elif isinstance(target, list):
                deny_tokens.setdefault(kind, set()).update(target)
            elif isinstance(target, dict):
                named = deny_named.setdefault(kind, set())
                for rname, names in target.items():
                    named.update((rname, aname) for aname in names)

    def conflict(std_name: str, kind: Kind, what: str) -> None:
        report.add(
            "13.11",
            Severity.ERROR,
            f"standing[{std_name}] enables {kind.value} {what}, which deny "
            f"covers — deny always wins (§6.2), so the grant is unsatisfiable",
        )

    for std in policy.standing:
        for kind, target in std.enables.items():
            tokens = deny_tokens.get(kind, set())
            named = deny_named.get(kind, set())
            if kind in deny_star:
                conflict(std.name, kind, "(kind denied by '*')")
                continue
            if target == "*":  # enables the whole kind
                if tokens or named:
                    conflict(std.name, kind, "'*'")
            elif isinstance(target, list):
                for token in target:
                    if token in tokens or any(a == token for _, a in named):
                        conflict(std.name, kind, repr(token))
            elif isinstance(target, dict):
                for rname, names in target.items():
                    for aname in names:
                        if (
                            (rname, aname) in named
                            or aname in tokens
                            or rname in tokens
                        ):
                            conflict(std.name, kind, f"{rname}.{aname}")


def _check_ambiguous_bare_allow(
    policy: Policy, registry: InMemoryRegistry, report: LintReport
) -> None:
    """§13.12 (v0.3, CS-012) — a bare action name in ``allow`` declared by more
    than one resource applies everywhere it is declared ⇒ WARN (use the
    ``{Entity: [names]}`` map form to disambiguate)."""
    resources = registry.file.resources
    for pmap in policy.allow:
        for kind, target in pmap.items():
            if not isinstance(target, list):
                continue
            for token in target:
                if token in resources:
                    continue  # a resource grant, not an action name
                declaring = sorted(
                    rname
                    for rname, rdef in resources.items()
                    if (cand := rdef.actions.get(token)) is not None
                    and cand.kind == kind
                )
                if len(declaring) > 1:
                    report.add(
                        "13.12",
                        Severity.WARN,
                        f"{kind.value} {token!r} is declared by multiple resources "
                        f"({', '.join(declaring)}); the bare-name allow grants all "
                        f"of them — use the map form to disambiguate (§6.1)",
                    )


def _check_dual_auth_quorum(policy: Policy, report: LintReport) -> None:
    """§13.13 (v0.3, CS-014) — ``dualAuthorization`` with an explicit
    ``quorum`` < 2 contradicts the gate's definition (§7.9) ⇒ ERROR."""
    for key, gateset in policy.gates.items():
        cfg = gateset.get("dualAuthorization")
        if isinstance(cfg, dict):
            quorum = cfg.get("quorum")
            if isinstance(quorum, int) and quorum < 2:
                report.add(
                    "13.13",
                    Severity.ERROR,
                    f"gates[{key}].dualAuthorization has quorum {quorum} — two "
                    f"distinct identities are the gate's definition (§7.9)",
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
