"""Reason-code retry classification (RFC §11, v0.6 CS-029).

One home for the normative classes: which built-in gate refusals are
``retryable``, which structural/settle reasons carry which class, and the
``terminal`` default for everything undeclared. Pure — part of the trust
kernel; the pipeline, transport, and settle paths all classify through here so
the agent-facing channel and the audit record can never disagree.
"""

from __future__ import annotations

from stonefold_core.enums import Decision, Outcome, RetryClass
from stonefold_core.models import GateResult

# Built-in gate refusals whose defect is in the INTENT — fix it and resubmit
# (RFC §11: valueLimit, rate, quota, quantityCap, spendLimit, window,
# contentCheck, requireExplanation). Everything else defaults terminal:
# allowlist/denylist, disclosure, precondition (per check code where declared),
# the transition from-states guard, and any gate not listed.
RETRYABLE_GATES: frozenset[str] = frozenset(
    {
        "valueLimit",
        "rate",
        "quota",
        "quantityCap",
        "spendLimit",
        "window",
        "contentCheck",
        "requireExplanation",
    }
)

# Structural decision/settle reasons with a declared class (RFC §11). Reasons
# absent from this table — including dependency failures like
# ``outbox-unavailable`` — default terminal: an outage is not an intent defect,
# and the safe direction is to stop retrying.
_EXACT: dict[str, RetryClass] = {
    "stale-decision": RetryClass.RETRYABLE,  # resubmit for a fresh decision
    "hold-unresolvable": RetryClass.ESCALATE,  # a config error humans must fix
    "unknown-action": RetryClass.TERMINAL,
    "default-deny": RetryClass.TERMINAL,
    "scope-denied": RetryClass.TERMINAL,
    "scope-unavailable": RetryClass.TERMINAL,
    "scope-lost": RetryClass.TERMINAL,
}

# Prefixed settle reasons: the world moved (re-decide) vs. the hold lapsed
# (a human question went unanswered — surface it) vs. an operator stop.
_PREFIXES: tuple[tuple[str, RetryClass], ...] = (
    ("stale-guard:", RetryClass.RETRYABLE),
    ("expired-hold:", RetryClass.ESCALATE),
    ("kill:", RetryClass.TERMINAL),
    ("deny", RetryClass.TERMINAL),  # authorize-step refusals ("deny:…")
)


def gate_class(gate: str) -> RetryClass:
    """The default class for a built-in gate's refusal (RFC §11, CS-029)."""
    return RetryClass.RETRYABLE if gate in RETRYABLE_GATES else RetryClass.TERMINAL


def rule_class(rule: str) -> RetryClass:
    """The class of a structural decision/settle reason string."""
    exact = _EXACT.get(rule)
    if exact is not None:
        return exact
    for prefix, cls in _PREFIXES:
        if rule.startswith(prefix):
            return cls
    return RetryClass.TERMINAL


def classify(
    decision: Decision, rule: str, gates: tuple[GateResult, ...]
) -> tuple[str, RetryClass | None]:
    """The (reason code, retry class) an ``EvalResult`` carries (CS-029).

    ALLOW carries neither. A gate-decided refusal (``rule == "gate:<name>"``)
    takes the deciding gate's code (falling back to the rule string) and its
    class (check-declared, else the gate's built-in default). A structural
    refusal uses the rule string as its code. An approval-shaped HOLD with no
    check code carries no class — the agent's move is to wait, which none of
    the three classes means.
    """
    if decision is Decision.ALLOW:
        return "", None
    if rule.startswith("gate:"):
        gate = rule[len("gate:") :]
        deciding = next(
            (g for g in reversed(gates) if g.gate == gate and g.outcome is not Outcome.PASS),
            None,
        )
        if deciding is not None:
            code = deciding.code or rule
            if deciding.retry_class is not None:
                return code, deciding.retry_class
            if deciding.outcome is Outcome.HOLD and not deciding.code:
                return code, None  # approval-shaped hold: wait, not retry
            return code, gate_class(gate)
        return rule, gate_class(gate)
    return rule, rule_class(rule)
