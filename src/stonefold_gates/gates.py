"""The fifteen deterministic gates (RFC §7, design §6).

Each is ``(cfg, GateContext) -> GateResult``. Stateless gates compute in-memory;
the four counter gates (``rate``/``quota``/``quantityCap``/``spendLimit``) read a
``CounterStore``; ``contentCheck`` calls a registered hook; ``requireMatch``
(v0.6 CS-032) queries a declared obligation registry. A *dependency* failure
(missing field, store down, hook timeout, registry unreachable) is turned into
**fail-closed FAIL** here — never a raised exception and never a silent pass
(CLAUDE.md, design §10/§12).

``failureMode: open`` (RFC §10) flips the dependency-failure cases to pass, at
two deliberately different strictnesses: the obligation-registry outage
additionally applies the **irreversible floor** (``should_fail_closed``) because
§7.16 semantics 4 mandates it, like the kill store (CS-007); the content-hook
and named-check crash paths honour ``open`` plainly, because there §10 alone
governs and the §13.5 linter refuses an open-mode policy with an irreversible
effect unless explicitly acknowledged — the floor lives in the linter, not here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from stonefold_core.condition import (
    NAMESPACES,
    Compare,
    ConditionError,
    ConditionRuntimeError,
    EvalContext,
    Expr,
    Func,
    InExpr,
    Literal,
    MissingValueError,
    Path,
    evaluate,
    parse,
    resolve_operand,
)
from stonefold_core.enums import Outcome, RetryClass
from stonefold_core.failure import should_fail_closed
from stonefold_core.models import GateResult
from stonefold_core.obligation import (
    MISSING,
    EqConstraint,
    Obligation,
    lookup_field,
)
from stonefold_core.policy import FailureMode
from stonefold_gates.base import (
    CheckResult,
    GateContext,
    failed,
    held,
    now_ts,
    parse_rate,
    passed,
    resolve_field,
    to_number,
    window_seconds,
)
from stonefold_gates.content import HookError

logger = logging.getLogger("stonefold.gates")

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


# --- 3. valueLimit -------------------------------------------------------
def value_limit(cfg: Any, gctx: GateContext) -> GateResult:
    field = cfg.get("field") if isinstance(cfg, dict) else None
    fields = (str(field),) if field else ()  # CS-030: what code+fields may reveal
    try:
        num = to_number(resolve_field(field, gctx))
    except (MissingValueError, ConditionRuntimeError) as exc:
        return failed("valueLimit", f"fail-closed: {exc}", fields=fields)
    mx, mn = cfg.get("max"), cfg.get("min")
    if mx is not None and num > float(mx):
        return failed("valueLimit", f"{field}={num} exceeds max {mx}", fields=fields)
    if mn is not None and num < float(mn):
        return failed("valueLimit", f"{field}={num} below min {mn}", fields=fields)
    return passed("valueLimit")


# --- 5. allowlist / denylist --------------------------------------------
def _membership(cfg: Any, gctx: GateContext, *, deny: bool) -> GateResult:
    name = "denylist" if deny else "allowlist"
    if not isinstance(cfg, dict):
        return failed(name, "fail-closed: gate needs {field, set}")
    field, set_name = cfg.get("field"), cfg.get("set")
    fields = (str(field),) if field else ()  # CS-030: what code+fields may reveal
    try:
        value = str(resolve_field(field, gctx))
    except (MissingValueError, ConditionRuntimeError) as exc:
        return failed(name, f"fail-closed: {exc}", fields=fields)
    if set_name is not None:
        members = set(gctx.registry.named_set(set_name))
    else:
        members = {str(v) for v in cfg.get("values", [])}
    in_set = value in members
    if deny:
        if in_set:
            return failed(name, f"{field}={value!r} is denylisted", fields=fields)
        return passed(name)
    if in_set:
        return passed(name)
    return failed(name, f"{field}={value!r} not in {set_name!r}", fields=fields)


def allowlist(cfg: Any, gctx: GateContext) -> GateResult:
    return _membership(cfg, gctx, deny=False)


def denylist(cfg: Any, gctx: GateContext) -> GateResult:
    return _membership(cfg, gctx, deny=True)


# --- 6. precondition (named checks + transition from-states) -------------
def check_from_states(from_states: Any, gctx: GateContext) -> GateResult:
    """The built-in transition guard (RFC §7.6): the target's current state MUST
    be one of the declared ``from`` states. Unknown state ⇒ fail-closed."""
    current = gctx.env.resource.get("currentState")
    if current is None:
        return failed("precondition", "fail-closed: target currentState unknown")
    allowed = tuple(from_states)
    if current in allowed:
        return passed("precondition")
    return failed("precondition", f"state {current!r} not in from-states {allowed}")


def _run_named_check(name: str, gctx: GateContext) -> CheckResult:
    """Run a registered precondition check, normalised to the three-valued
    ``CheckResult`` (RFC §7.6, CS-026). A plain-``bool`` check stays valid.
    POC convention (STONEFOLD-AMBIGUITY, RFC §7.6): with no registered
    implementation the check passes iff the call carries a boolean flag of the
    same name set ``true`` — deterministic and test-drivable; a real deployment
    registers code here."""
    check = gctx.preconditions.get(name)
    if check is not None:
        result = check(gctx)
        if isinstance(result, CheckResult):
            return result
        return CheckResult(Outcome.PASS if result else Outcome.FAIL)
    passes = gctx.resolved.data.get(name) is True
    return CheckResult(Outcome.PASS if passes else Outcome.FAIL)


def _run_checks(gate: str, names: list[Any], gctx: GateContext) -> GateResult:
    """Shared check-runner for ``precondition``/``emissionControl`` (CS-026).

    Verdict composition within one gate: any FAIL wins (a cheap deterministic
    refusal beats a human interruption); else the first HOLD holds; else pass.
    Guardrails enforced here, not left to check authors' goodwill: a crash is a
    dependency failure under §10 (fail-closed unless ``failureMode: open``),
    never a hold; a hold without a machine-readable reason code is a check
    implementation error — resolved fail-closed, logged loudly.
    """
    first_hold: tuple[str, CheckResult] | None = None
    for raw in names:
        name = str(raw)
        try:
            result = _run_named_check(name, gctx)
        except Exception as exc:  # crash ⇒ dependency failure, NEVER a hold (I5)
            # plain §10 ``open`` — no irreversible floor here by design: the
            # §13.5 linter refuses open-mode + irreversible (module docstring).
            if gctx.failure_mode is FailureMode.OPEN:
                continue
            return failed(gate, f"fail-closed: {name} errored: {exc}", source=name)
        if result.outcome is Outcome.FAIL:
            return failed(
                gate,
                f"{name} not satisfied",
                code=result.code,
                source=name,
                # CS-029: the code's class comes from the check's registry
                # declaration; undeclared/code-less ⇒ the engine's default.
                retry_class=(
                    gctx.registry.reason_class(name, result.code) if result.code else None
                ),
            )
        if result.outcome is Outcome.HOLD:
            if not result.code:
                # CS-026 rule 2 / I4: an uninformative interruption is worse
                # than a deny — treat as an implementation error, fail closed.
                logger.error(
                    "check %r returned hold without a reason code (CS-026); "
                    "resolving fail-closed", name,
                )
                return failed(
                    gate, f"fail-closed: {name} held without a reason code", source=name
                )
            if not gctx.registry.check_hold_capable(name):
                # CS-026 rule 3: hold capability is declared in the registry
                # (docs/06 §5). An undeclared hold is an implementation error.
                logger.error(
                    "check %r returned hold but is not declared holdCapable "
                    "(CS-026); resolving fail-closed", name,
                )
                return failed(
                    gate,
                    f"fail-closed: {name} held without declared hold capability",
                    source=name,
                )
            if first_hold is None:
                first_hold = (name, result)
    if first_hold is not None:
        name, result = first_hold
        return held(
            gate,
            f"{name}: {result.code}",
            code=result.code,
            source=name,
            evidence=dict(result.evidence) if result.evidence is not None else None,
            retry_class=gctx.registry.reason_class(name, result.code),
        )
    return passed(gate)


def precondition(cfg: Any, gctx: GateContext) -> GateResult:
    if isinstance(cfg, dict) and "from" in cfg:
        return check_from_states(cfg["from"], gctx)
    if isinstance(cfg, dict):
        names = list(cfg.get("checks", []))
    else:
        names = cfg if isinstance(cfg, list) else [cfg]
    return _run_checks("precondition", names, gctx)


# --- 7. contentCheck -----------------------------------------------------
def content_check(cfg: Any, gctx: GateContext) -> GateResult:
    names = cfg if isinstance(cfg, list) else [cfg]
    for name in names:
        try:
            clean = gctx.hooks.run(str(name), gctx.resolved.data)
        except HookError as exc:
            # timeout/error ⇒ apply failureMode (design §12). C7: closed ⇒ block.
            # Plain §10 ``open`` — no irreversible floor here by design: the
            # §13.5 linter refuses open-mode + irreversible (module docstring).
            if gctx.failure_mode is FailureMode.OPEN:
                continue
            return failed("contentCheck", f"fail-closed: {exc}")
        if not clean:
            return failed("contentCheck", f"{name} blocked the content")
    return passed("contentCheck")


# --- 8/9. requireApproval / dualAuthorization (HOLD) ---------------------
def require_approval(cfg: Any, gctx: GateContext) -> GateResult:
    # Reaching here means any `when:` was true (engine-handled): approval is due.
    # Staging the PENDING_APPROVAL row is M4; here we only signal HOLD.
    return held("requireApproval", "human approval required")


def dual_authorization(cfg: Any, gctx: GateContext) -> GateResult:
    return held("dualAuthorization", "two distinct approvals required")


# --- 10. window ----------------------------------------------------------
def window_gate(cfg: Any, gctx: GateContext) -> GateResult:
    from stonefold_core.condition import make_window  # local: avoids import at module load

    now = gctx.env.now
    if now is None:
        return failed("window", "fail-closed: no clock")
    if not isinstance(cfg, dict):
        return failed("window", "fail-closed: gate needs {days?, hours?}")
    days = cfg.get("days")
    if days:
        day_name = _WEEKDAYS[now.weekday()]
        if day_name not in days:
            return failed("window", f"{day_name} not in allowed days")
    hours = cfg.get("hours")
    if hours:
        try:
            rng = make_window(hours)
        except ConditionRuntimeError as exc:
            return failed("window", f"fail-closed: {exc}")
        if now not in rng:
            return failed("window", "outside allowed hours")
    return passed("window")


# --- 11. quantityCap -----------------------------------------------------
def quantity_cap(cfg: Any, gctx: GateContext) -> GateResult:
    if not isinstance(cfg, dict):
        return failed("quantityCap", "fail-closed: gate needs {per, limit, window}")
    try:
        limit = int(cfg["limit"])
        win = window_seconds(cfg["window"])
        now = now_ts(gctx)
        subject: list[str] = []
        if cfg.get("per"):
            subject.append(str(resolve_field(cfg["per"], gctx)))
        if cfg.get("of"):
            subject.append(str(resolve_field(cfg["of"], gctx)))
        key = f"{gctx.agent}:quantityCap:{gctx.resolved.action}:{':'.join(subject)}"
        count = gctx.counters.hit(key, now, win)
    except (MissingValueError, ConditionRuntimeError, KeyError) as exc:
        return failed("quantityCap", f"fail-closed: {exc}")
    except Exception as exc:  # counter store unreachable ⇒ fail closed (§13)
        return failed("quantityCap", f"fail-closed: counter store unavailable: {exc}")
    if count > limit:
        return failed("quantityCap", f"quantity cap {limit} exceeded ({count}) for subject")
    return passed("quantityCap")


# --- 12. disclosure ------------------------------------------------------
def disclosure(cfg: Any, gctx: GateContext) -> GateResult:
    """Pre-check form (design §6 review note): the action's sensitivity is known
    from the registry, so we can block *before* execution when the requested sink
    is not permitted. The ``when:`` (e.g. sensitivity == restricted) is evaluated
    by the engine; reaching here means the result is sensitive.

    ``maxClassification`` (CS-024) compares the action's declared
    ``resultSensitivity`` against a ceiling, by the registry's DECLARED
    classification order (built-in ``public < internal < confidential <
    restricted``; a domain's substituted labels are ordered by their value-set
    position). The ceiling is a literal label or a condition path (e.g.
    ``actor.clearance``); either side missing from the declared order fails
    closed (RFC §8)."""
    if isinstance(cfg, dict) and cfg.get("maxClassification") is not None:
        verdict = _classification_check(cfg["maxClassification"], gctx)
        if verdict is not None:
            return verdict
    return _disclosure_decide(cfg, gctx.env.sink)


def _classification_check(ceiling_ref: Any, gctx: GateContext) -> GateResult | None:
    """FAIL when the action's sensitivity exceeds the ceiling or either label is
    outside the declared order (fail closed, CS-024); ``None`` to fall through to
    the sink check."""
    ceiling = ceiling_ref
    if not isinstance(ceiling, str):
        return failed("disclosure", f"fail-closed: bad maxClassification {ceiling_ref!r}")
    if gctx.registry.classification_rank(ceiling) is None:
        # not a literal label — resolve it as a condition path (§7.12's
        # ``maxClassification: actor.clearance`` form); unresolvable ⇒ fail closed
        try:
            ceiling = str(resolve_field(ceiling, gctx))
        except (MissingValueError, ConditionRuntimeError) as exc:
            return failed("disclosure", f"fail-closed: {exc}")
    max_rank = gctx.registry.classification_rank(ceiling)
    if max_rank is None:
        return failed(
            "disclosure",
            f"fail-closed: classification {ceiling!r} not in the declared order",
        )
    sensitivity = gctx.resolved.attrs.resultSensitivity
    rank = gctx.registry.classification_rank(sensitivity)
    if rank is None:
        return failed(
            "disclosure",
            f"fail-closed: classification {sensitivity!r} not in the declared order",
        )
    if rank > max_rank:
        return failed(
            "disclosure", f"result classification {sensitivity!r} exceeds {ceiling!r}"
        )
    return None


def disclosure_post_check(
    result_sensitivity: str, cfg: Any, *, sink: str | None
) -> GateResult:
    """Post-check form: called on the *return* path once the read's result
    sensitivity is known (row-level). On a non-permitted sink the gateway drops
    the result and returns a refusal — "read executed, result withheld" (C6)."""
    return _disclosure_decide(cfg, sink, withheld=result_sensitivity)


def _disclosure_decide(cfg: Any, sink: str | None, withheld: str | None = None) -> GateResult:
    allow_sink = cfg.get("allowSink") if isinstance(cfg, dict) else None
    if allow_sink is not None and (sink is None or sink not in allow_sink):
        suffix = f" (result '{withheld}' withheld)" if withheld else ""
        return failed("disclosure", f"sink {sink!r} not in allowSink{suffix}")
    return passed("disclosure")


# --- 13. emissionControl -------------------------------------------------
def emission_control(cfg: Any, gctx: GateContext) -> GateResult:
    # RFC §7.13 / CS-011: the gate's check list is spelled ``checks:``. The
    # previous read of a ``precondition:`` key (never legal syntax) silently
    # skipped every declared check — fixed in v0.6 alongside CS-026; the legacy
    # key is still honoured so no deployed config loosens.
    checks: list[Any] = []
    if isinstance(cfg, dict):
        checks = list(cfg.get("checks") or cfg.get("precondition") or [])
    result = _run_checks("emissionControl", checks, gctx)
    if result.outcome is not Outcome.PASS:
        if result.outcome is Outcome.FAIL and result.source:
            return failed(
                "emissionControl",
                f"deconfliction failed: {result.source}",
                code=result.code,
                source=result.source,
            )
        return result
    if isinstance(cfg, dict) and cfg.get("holdForAuthorization"):
        if gctx.resolved.data.get("emissionAuthorized") is not True:
            return held("emissionControl", "awaiting emission authorization")
    return passed("emissionControl")


# --- 14. requireExplanation ----------------------------------------------
def require_explanation(cfg: Any, gctx: GateContext) -> GateResult:
    if cfg is False:
        return passed("requireExplanation")
    expl = gctx.resolved.data.get("explanation")
    if isinstance(expl, str) and expl.strip():
        return passed("requireExplanation")
    return failed("requireExplanation", "action carries no recorded rationale")


# --- 15. requireMatch (v0.6 CS-032/033/036) --------------------------------
_MATCH = "requireMatch"
_OBLIGATION_NS = "obligation"


@dataclass(frozen=True)
class _StringClause:
    """One parsed §8-grammar conjunct of ``match``/``provenance``. ``eq`` is
    set when the clause is selector-eligible — ``obligation.X == <intent-side>``
    — carrying the record-relative field and the intent-side operand to resolve
    at decision time (RFC §7.16 semantics 1). Every string clause, selector or
    not, is re-evaluated against the matched record (defence in depth: an
    adapter returning a non-matching record still fails closed)."""

    src: str
    expr: Expr
    eq: "tuple[str, Any] | None"
    intent_fields: tuple[str, ...]
    provenance: bool


@dataclass(frozen=True)
class _ToleranceClause:
    """A structured tolerance conjunct (CS-033): ``field`` (record-relative)
    must equal ``matches`` (intent side) within ``pct`` percent of the
    obligation-side value or ``abs_`` in the field's unit; 0 means exact."""

    field: str
    matches: str
    pct: float | None
    abs_: float | None
    provenance: bool


def _operand_paths(op: Any, out: list[Path]) -> None:
    if isinstance(op, Path):
        out.append(op)
    elif isinstance(op, Func):
        for arg in op.args:
            _operand_paths(arg, out)


def _expr_paths(node: Any) -> list[Path]:
    out: list[Path] = []
    stack: list[Any] = [node]
    while stack:
        n = stack.pop()
        if isinstance(n, (Compare,)):
            _operand_paths(n.left, out)
            _operand_paths(n.right, out)
        elif isinstance(n, InExpr):
            _operand_paths(n.left, out)
            if not isinstance(n.right, Literal):
                _operand_paths(n.right, out)
        elif hasattr(n, "left") and hasattr(n, "right"):  # And / Or
            stack.append(n.left)
            stack.append(n.right)
        elif hasattr(n, "expr"):  # Not
            stack.append(n.expr)
        elif hasattr(n, "path"):  # Exists
            out.append(n.path)
    return out


def _is_obligation_path(op: Any) -> bool:
    return isinstance(op, Path) and len(op.parts) > 1 and op.parts[0] == _OBLIGATION_NS


def _refs_obligation(op: Any) -> bool:
    paths: list[Path] = []
    _operand_paths(op, paths)
    return any(p.parts[0] == _OBLIGATION_NS for p in paths)


def _intent_fields(expr_paths: list[Path]) -> tuple[str, ...]:
    """The intent-side namespace paths a clause compares (CS-030: what
    ``code+fields`` visibility may reveal)."""
    return tuple(
        ".".join(p.parts)
        for p in expr_paths
        if len(p.parts) > 1 and p.parts[0] in NAMESPACES
    )


def _parse_within(raw: Any) -> tuple[float | None, float | None]:
    """``within`` (CS-033): ``"N%"`` relative or a non-negative absolute
    number. Raises ``ValueError`` on any other shape (fail closed)."""
    if isinstance(raw, str) and raw.endswith("%"):
        return float(raw[:-1]), None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw >= 0:
        return None, float(raw)
    raise ValueError(f"bad within {raw!r}")


def _parse_clause(raw: Any, *, provenance: bool) -> "_StringClause | _ToleranceClause":
    """Parse one ``match``/``provenance`` entry. Raises ``ValueError`` /
    ``ConditionError`` on a malformed clause — the gate fails closed."""
    if isinstance(raw, str):
        expr = parse(raw)
        paths = _expr_paths(expr)
        eq: tuple[str, Any] | None = None
        if isinstance(expr, Compare) and expr.op == "==":
            left_obl = _is_obligation_path(expr.left)
            right_obl = _is_obligation_path(expr.right)
            if left_obl != right_obl:
                obl_side, other = (
                    (expr.left, expr.right) if left_obl else (expr.right, expr.left)
                )
                if not _refs_obligation(other):
                    assert isinstance(obl_side, Path)
                    eq = (".".join(obl_side.parts[1:]), other)
        return _StringClause(
            src=raw, expr=expr, eq=eq,
            intent_fields=_intent_fields(paths), provenance=provenance,
        )
    if isinstance(raw, dict):
        field_path = raw.get("field")
        matches = raw.get("matches")
        if not (isinstance(field_path, str) and field_path.startswith("obligation.")):
            raise ValueError(f"tolerance field must be obligation.*, got {field_path!r}")
        if not isinstance(matches, str) or not matches:
            raise ValueError(f"bad tolerance matches {matches!r}")
        pct, abs_ = _parse_within(raw.get("within"))
        return _ToleranceClause(
            field=field_path[len("obligation.") :], matches=matches,
            pct=pct, abs_=abs_, provenance=provenance,
        )
    raise ValueError(f"bad match clause {raw!r}")


def _registry_unavailable(reg: str, detail: str, gctx: GateContext) -> GateResult:
    """An unreachable/unregistered obligation registry is a dependency failure
    (RFC §10): ``failureMode`` decides, with the irreversible floor —
    an irreversible effect MUST resolve closed (§7.16 semantics 4)."""
    if should_fail_closed(gctx.resolved, gctx.failure_mode):
        return failed(_MATCH, f"fail-closed: obligation registry {reg!r} unavailable: {detail}")
    return passed(_MATCH, f"failureMode=open: obligation registry {reg!r} unavailable, gate skipped")


def _check_tolerance(
    clause: _ToleranceClause, ob: Obligation, gctx: GateContext,
    evidence: dict[str, Any],
) -> GateResult | None:
    """Evaluate one tolerance conjunct against the matched record. ``None`` ⇒
    within tolerance; a ``GateResult`` ⇒ the deciding failure."""
    fields = (clause.matches,)
    record_side = lookup_field(ob.fields, clause.field)
    if record_side is MISSING or record_side is None:
        # CS-032 semantics 4: an obligation path absent or null on the matched
        # record fails the gate closed.
        return failed(
            _MATCH, f"fail-closed: obligation.{clause.field} absent/null on matched record",
            evidence=evidence, fields=fields,
        )
    try:
        obl_num = to_number(record_side)
        intent_num = to_number(resolve_field(clause.matches, gctx))
    except (MissingValueError, ConditionRuntimeError) as exc:
        return failed(_MATCH, f"fail-closed: {exc}", evidence=evidence, fields=fields)
    delta = abs(obl_num - intent_num)
    limit = (
        clause.pct / 100.0 * abs(obl_num) if clause.pct is not None
        else (clause.abs_ if clause.abs_ is not None else 0.0)
    )
    if delta > limit:
        within = f"{clause.pct:g}%" if clause.pct is not None else f"{clause.abs_:g}"
        return failed(
            _MATCH,
            f"{clause.matches} outside tolerance {within} of obligation.{clause.field}",
            code="outside-tolerance", retry_class=RetryClass.RETRYABLE,
            evidence=evidence, fields=fields,
        )
    return None


def require_match(cfg: Any, gctx: GateContext) -> GateResult:
    """Gate 15 (RFC §7.16, v0.6 CS-032/033/036): the intent must correspond to
    exactly one open obligation in a declared registry, within declared
    tolerance. Deterministic at decision time: the gateway derives a typed
    selector from the ``match`` conjunction's equality clauses, queries the
    adapter, and decides on the candidate count — 0 ⇒ ``onNoMatch``, >1 ⇒
    ``onAmbiguous`` (never auto-selects), 1 ⇒ the full conjunction plus
    tolerance and ``provenance`` evaluate against the RE-READ record only
    (CS-036: agent-supplied copies of obligation data are never match inputs;
    a ``data.*`` pointer is just another equality clause — it narrows the
    query, never substitutes for it)."""
    if not isinstance(cfg, dict):
        return failed(_MATCH, "fail-closed: gate needs {registry, match, consume}")
    reg_name = str(cfg.get("registry") or "")
    consume = cfg.get("consume")
    if not (isinstance(consume, str) and (consume == "none" or consume.startswith("obligation."))):
        return failed(_MATCH, f"fail-closed: consume must be an obligation.* path or 'none', got {consume!r}")
    if cfg.get("onAmbiguous") == "allow":
        # illegal value (§13 rule 17); a policy that somehow loaded with it must
        # not make the gateway auto-select among candidates.
        return failed(_MATCH, "fail-closed: onAmbiguous: allow is illegal (RFC §7.16)")
    decl = gctx.registry.obligation_registry(reg_name)
    if decl is None:
        return failed(_MATCH, f"fail-closed: unknown obligation registry {reg_name!r}")

    raw_match = cfg.get("match")
    if not isinstance(raw_match, list) or not raw_match:
        return failed(_MATCH, "fail-closed: match must be a non-empty list")
    try:
        clauses: list[_StringClause | _ToleranceClause] = [
            _parse_clause(c, provenance=False) for c in raw_match
        ]
        clauses += [
            _parse_clause(c, provenance=True) for c in (cfg.get("provenance") or [])
        ]
    except (ValueError, ConditionError) as exc:
        return failed(_MATCH, f"fail-closed: bad match clause: {exc}")

    all_fields = tuple(
        dict.fromkeys(  # ordered de-dup
            f
            for c in clauses
            for f in (c.intent_fields if isinstance(c, _StringClause) else (c.matches,))
        )
    )

    # The typed selector: match-conjunction equality clauses made concrete by
    # resolving their intent side now (provenance never narrows the query — it
    # binds the matched record's counterparty to the intent's evidence AFTER
    # identification, RFC §7.16).
    selector: list[EqConstraint] = []
    for clause in clauses:
        if isinstance(clause, _StringClause) and clause.eq is not None and not clause.provenance:
            rel_path, operand = clause.eq
            try:
                value = resolve_operand(operand, gctx.eval_ctx)
            except (MissingValueError, ConditionRuntimeError) as exc:
                return failed(_MATCH, f"fail-closed: {exc}", fields=clause.intent_fields)
            selector.append(EqConstraint(field=rel_path, value=value))

    adapter = gctx.obligations.get(reg_name)
    if adapter is None:
        return _registry_unavailable(reg_name, "no adapter registered", gctx)
    try:
        candidates = tuple(adapter.query(tuple(selector)))
    except Exception as exc:  # registry unreachable ⇒ dependency failure (§10)
        return _registry_unavailable(reg_name, str(exc), gctx)

    count = len(candidates)
    if count == 0:
        evidence: dict[str, Any] = {"registry": reg_name, "refs": [], "candidates": 0}
        if cfg.get("onNoMatch") == "hold":
            return held(
                _MATCH, "no obligation matches the intent", code="no-match",
                evidence=evidence, fields=all_fields,
            )
        return failed(
            _MATCH, "no obligation matches the intent", code="no-match",
            retry_class=RetryClass.TERMINAL, evidence=evidence, fields=all_fields,
        )
    if count > 1:
        evidence = {
            "registry": reg_name,
            "refs": [c.ref for c in candidates],
            "candidates": count,
        }
        if cfg.get("onAmbiguous", "hold") == "deny":
            return failed(
                _MATCH, f"{count} obligations match; the gateway never auto-selects",
                code="ambiguous-match", retry_class=RetryClass.ESCALATE,
                evidence=evidence, fields=all_fields,
            )
        return held(
            _MATCH, f"{count} obligations match; a human must decide",
            code="ambiguous-match", evidence=evidence, fields=all_fields,
        )

    ob = candidates[0]
    # A PASS additionally carries the consumption PLAN (CS-035): the staging
    # commit reads consume/capability from here to reserve the matched ref
    # before the commit returns. Deny/hold evidence stays lineage-only.
    evidence = {
        "registry": reg_name, "refs": [ob.ref], "candidates": 1,
        "consume": consume, "capability": decl.capability.value,
    }
    # CS-036 by construction: the ``obligation`` namespace is populated
    # exclusively from the adapter's response — a forged copy in ``data.*``
    # is just another intent field and changes nothing.
    ext_ctx = EvalContext(
        namespaces={**dict(gctx.eval_ctx.namespaces), _OBLIGATION_NS: dict(ob.fields)},
        functions=gctx.eval_ctx.functions,
    )
    for clause in clauses:
        if isinstance(clause, _ToleranceClause):
            verdict = _check_tolerance(clause, ob, gctx, evidence)
            if verdict is not None:
                if clause.provenance and verdict.code == "outside-tolerance":
                    verdict = verdict.model_copy(
                        update={"code": "provenance-mismatch",
                                "retry_class": RetryClass.TERMINAL},
                    )
                return verdict
            continue
        try:
            ok = evaluate(clause.expr, ext_ctx)
        except (MissingValueError, ConditionRuntimeError) as exc:
            # an ``obligation.*`` path absent on the matched record lands here
            # (CS-032 semantics 4) — fail closed, like any resolution error.
            return failed(
                _MATCH, f"fail-closed: {exc}", evidence=evidence,
                fields=clause.intent_fields,
            )
        if not ok:
            if clause.provenance:
                return failed(
                    _MATCH, f"provenance clause failed: {clause.src}",
                    code="provenance-mismatch", retry_class=RetryClass.TERMINAL,
                    evidence=evidence, fields=clause.intent_fields,
                )
            return failed(
                _MATCH, f"match clause failed against the matched record: {clause.src}",
                code="match-failed", retry_class=RetryClass.TERMINAL,
                evidence=evidence, fields=clause.intent_fields,
            )
    return GateResult(gate=_MATCH, outcome=Outcome.PASS, evidence=evidence)


# --- 1/2/4. rate / quota / spendLimit (counter gates) --------------------
def rate(cfg: Any, gctx: GateContext) -> GateResult:
    return _count_gate("rate", cfg, gctx)


def quota(cfg: Any, gctx: GateContext) -> GateResult:
    return _count_gate("quota", cfg, gctx)


def _count_gate(name: str, cfg: Any, gctx: GateContext) -> GateResult:
    """Shared body for ``rate`` and ``quota``: count hits in a sliding window,
    optionally scoped by ``per:``."""
    spec = cfg.get("limit") if isinstance(cfg, dict) else cfg
    per = cfg.get("per") if isinstance(cfg, dict) else None
    try:
        limit, win = parse_rate(spec)
        now = now_ts(gctx)
        suffix = str(resolve_field(per, gctx)) if per else ""
        key = f"{gctx.agent}:{name}:{gctx.resolved.action}:{suffix}"
        count = gctx.counters.hit(key, now, win)
    except (MissingValueError, ConditionRuntimeError) as exc:
        return failed(name, f"fail-closed: {exc}")
    except Exception as exc:
        return failed(name, f"fail-closed: counter store unavailable: {exc}")
    if count > limit:
        return failed(name, f"{name} limit {limit:g} exceeded ({count})")
    return passed(name)


def spend_limit(cfg: Any, gctx: GateContext) -> GateResult:
    spec = cfg.get("limit") if isinstance(cfg, dict) else cfg
    try:
        limit, win = parse_rate(spec)
        now = now_ts(gctx)
        amount = (
            gctx.env.cost
            if gctx.env.cost is not None
            else float(gctx.resolved.data.get("_cost", 1.0))
        )
        # spend is accumulated per session/agent, NOT per action.
        key = f"{gctx.agent}:spendLimit:{gctx.session.id}"
        total = gctx.counters.add(key, amount, now, win)
    except (MissingValueError, ConditionRuntimeError) as exc:
        return failed("spendLimit", f"fail-closed: {exc}")
    except Exception as exc:
        return failed("spendLimit", f"fail-closed: counter store unavailable: {exc}")
    if total > limit:
        return failed("spendLimit", f"session spend {total:g} exceeds {limit:g}")
    return passed("spendLimit")
