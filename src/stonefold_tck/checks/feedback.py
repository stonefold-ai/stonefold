"""K1–K3 — the agent feedback channel (v0.6 CS-029/030; profile ``feedback``).

A deny/hold is a convergence signal: it carries a machine-readable code and a
retry class. Visibility is a declared choice — the default ``code+fields``
keeps the loop convergent without handing over record-side values — and
redaction happens on the RETURN path only: the audit record keeps everything.
"""

from __future__ import annotations

from stonefold_tck.checks import PROFILE_FEEDBACK, check, expect
from stonefold_tck.checks._util import expect_decision, pay, setup, submit
from stonefold_tck.driver import (
    CAP_AUDIT,
    CAP_FEEDBACK,
    CAP_OBLIGATION,
    ConformanceDriver,
    Operation,
)
from stonefold_tck.fixtures import POLICY_MATCH

# a distinctive record-side value: if it appears anywhere in the agent-facing
# result, the channel leaked what the intent was compared AGAINST.
_SENTINEL_AMOUNT = 87650.0


@check(
    "K1",
    "deny results carry a machine-readable code and a retry class",
    PROFILE_FEEDBACK,
    requires=[CAP_FEEDBACK],
)
def k1_codes_and_classes(driver: ConformanceDriver) -> None:
    setup(driver)
    over_limit = expect_decision(submit(driver, pay(20000)), "deny", "over the value limit")
    expect(bool(over_limit.reason_code), "a deny must carry a reason code (CS-029)")
    expect(
        over_limit.retry_class == "retryable",
        f"a valueLimit deny is fixable in the intent — expected class 'retryable', "
        f"got {over_limit.retry_class!r}",
    )
    sanctioned = expect_decision(
        submit(driver, pay(500, country="XX")), "deny", "sanctioned destination"
    )
    expect(
        sanctioned.retry_class == "terminal",
        f"a denylist deny is not the agent's to fix — expected class 'terminal', "
        f"got {sanctioned.retry_class!r}",
    )


@check(
    "K2",
    "code+fields never leaks record-side values to the agent",
    PROFILE_FEEDBACK,
    requires=[CAP_FEEDBACK, CAP_OBLIGATION],
)
def k2_no_record_side_leak(driver: ConformanceDriver) -> None:
    setup(driver, policy=POLICY_MATCH, seed_world=True)
    driver.seed_obligations(
        "tck.orders",
        {"ORD-K2": {"vendorId": "PY1", "state": "open",
                    "line": {"amount": _SENTINEL_AMOUNT, "state": "unconsumed"}}},
    )
    # outside the 10% tolerance of the order's (secret) amount ⇒ deny.
    result = expect_decision(
        submit(driver, Operation(
            resource="Payment", action="pay", target="P1",
            data={"amount": 42000.0, "destinationCountry": "SK", "payeeId": "PY1"},
        )),
        "deny", "outside tolerance",
    )
    expect(bool(result.reason_code), "the deny must still carry its code")
    expect(
        "87650" not in result.agent_view,
        "the record-side amount the intent was compared against leaked into "
        "the agent-facing result (CS-030: code+fields never carries record-side values)",
    )
    expect(
        "ORD-K2" not in result.agent_view,
        "the matched obligation's ref (record-side evidence) leaked into the "
        "agent-facing result at the code+fields default (CS-030)",
    )


@check(
    "K3",
    "the audit record is unaffected by agent-facing redaction",
    PROFILE_FEEDBACK,
    requires=[CAP_FEEDBACK, CAP_AUDIT],
)
def k3_audit_unaffected(driver: ConformanceDriver) -> None:
    setup(driver)
    result = expect_decision(
        submit(driver, pay(500, country="XX")), "deny", "sanctioned destination"
    )
    expect(
        "denylisted" not in result.agent_view.lower(),
        "the prose gate reason leaked into the agent-facing result at the "
        "code+fields default (CS-030)",
    )
    denies = [r for r in driver.audit() if r.decision == "deny"]
    expect(bool(denies), "the refusal left no audit record")
    expect(
        bool(denies[-1].reason),
        "the audit record lost the deciding rule — redact on return, never on write",
    )
