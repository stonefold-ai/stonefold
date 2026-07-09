"""The fourteen deterministic gates (RFC §7, design §6).

Each is ``(cfg, GateContext) -> GateResult``. Stateless gates compute in-memory;
the four counter gates (``rate``/``quota``/``quantityCap``/``spendLimit``) read a
``CounterStore``; ``contentCheck`` calls a registered hook. A *dependency*
failure (missing field, store down, hook timeout) is turned into **fail-closed
FAIL** here — never a raised exception and never a silent pass (CLAUDE.md,
design §10/§12). ``failureMode: open`` flips the content-hook case to pass.
"""

from __future__ import annotations

import logging
from typing import Any

from stonefold_core.condition import ConditionRuntimeError, MissingValueError
from stonefold_core.enums import Outcome
from stonefold_core.models import GateResult
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
    try:
        num = to_number(resolve_field(field, gctx))
    except (MissingValueError, ConditionRuntimeError) as exc:
        return failed("valueLimit", f"fail-closed: {exc}")
    mx, mn = cfg.get("max"), cfg.get("min")
    if mx is not None and num > float(mx):
        return failed("valueLimit", f"{field}={num} exceeds max {mx}")
    if mn is not None and num < float(mn):
        return failed("valueLimit", f"{field}={num} below min {mn}")
    return passed("valueLimit")


# --- 5. allowlist / denylist --------------------------------------------
def _membership(cfg: Any, gctx: GateContext, *, deny: bool) -> GateResult:
    name = "denylist" if deny else "allowlist"
    if not isinstance(cfg, dict):
        return failed(name, "fail-closed: gate needs {field, set}")
    field, set_name = cfg.get("field"), cfg.get("set")
    try:
        value = str(resolve_field(field, gctx))
    except (MissingValueError, ConditionRuntimeError) as exc:
        return failed(name, f"fail-closed: {exc}")
    if set_name is not None:
        members = set(gctx.registry.named_set(set_name))
    else:
        members = {str(v) for v in cfg.get("values", [])}
    in_set = value in members
    if deny:
        return failed(name, f"{field}={value!r} is denylisted") if in_set else passed(name)
    return passed(name) if in_set else failed(name, f"{field}={value!r} not in {set_name!r}")


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
            if gctx.failure_mode is FailureMode.OPEN:
                continue
            return failed(gate, f"fail-closed: {name} errored: {exc}", source=name)
        if result.outcome is Outcome.FAIL:
            return failed(
                gate,
                f"{name} not satisfied",
                code=result.code,
                source=name,
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
