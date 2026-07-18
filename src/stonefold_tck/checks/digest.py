# SPDX-License-Identifier: Apache-2.0
"""DIGEST profile — connector digest pinning (v0.5 CS-020; registry §5, RFC §10).

The TCK authors the pin itself: ``connector_digest`` reports the digest of the
implementation actually loaded (computed the way the gateway verifies it), the
check splices it into the fixture registry, and ``tamper_connector`` simulates
the supply-chain replacement the pin defends against. The dispatch-time
mismatch settle reason ``connector-digest-mismatch`` is normative for a driver
claiming this capability (like the v0.4 freshness reasons).

Failure messages state the violation observed, not the expectation.
"""

from __future__ import annotations

from stonefold_tck.checks import PROFILE_DIGEST, check, expect
from stonefold_tck.checks._util import effects_of, pay, setup, submit
from stonefold_tck.driver import CAP_AUDIT, CAP_DIGEST, CAP_STAGING, ConformanceDriver
from stonefold_tck.fixtures import TCK_POLICY, TCK_REGISTRY

_UNPINNED = "tck-effects: { type: method }"
DIGEST_MISMATCH_REASON = "connector-digest-mismatch"


def _pinned_registry(digest: str) -> str:
    pinned = TCK_REGISTRY.replace(
        _UNPINNED, f'tck-effects: {{ type: method, digest: "{digest}" }}'
    )
    if pinned == TCK_REGISTRY:  # the fixture text moved — fail loudly, not vacuously
        raise AssertionError("digest check could not pin the fixture connector")
    return pinned


@check("I1", "a pinned digest mismatch fails closed at policy load", PROFILE_DIGEST,
       requires=[CAP_DIGEST])
def i1_load_time_mismatch_refuses(driver: ConformanceDriver) -> None:
    garbage = "sha256:" + "0" * 64
    result = driver.load(_pinned_registry(garbage), TCK_POLICY)
    expect(
        not result.ok,
        "a registry pinning a digest the loaded connector does not match "
        "still loaded (must fail closed, RFC §10)",
    )


@check("I2", "a dispatch-time digest mismatch cancels the staged effect, audited",
       PROFILE_DIGEST, requires=[CAP_DIGEST, CAP_STAGING, CAP_AUDIT])
def i2_dispatch_time_mismatch_cancels(driver: ConformanceDriver) -> None:
    setup(driver)  # plain load first, so the driver can report the real digest
    setup(driver, registry=_pinned_registry(driver.connector_digest("tck-effects")))
    r = submit(driver, pay(500))
    expect(r.decision == "allow" and r.ticket is not None,
           "a correctly pinned connector did not decide and stage normally")
    driver.tamper_connector("tck-effects")
    driver.dispatch_once()
    expect(
        effects_of(driver, "pay") == 0,
        "an effect left through a connector that no longer matches its pin",
    )
    reasons = [a.reason for a in driver.audit()]
    expect(
        any(DIGEST_MISMATCH_REASON in reason for reason in reasons),
        f"the cancellation lacks the normative {DIGEST_MISMATCH_REASON!r} reason",
    )


@check("I3", "a matching pin dispatches normally (no false refusals)", PROFILE_DIGEST,
       requires=[CAP_DIGEST, CAP_STAGING])
def i3_matching_pin_dispatches(driver: ConformanceDriver) -> None:
    setup(driver)
    setup(driver, registry=_pinned_registry(driver.connector_digest("tck-effects")))
    r = submit(driver, pay(500))
    expect(r.decision == "allow", "a small pay through the pinned connector was refused")
    driver.dispatch_once()
    expect(
        effects_of(driver, "pay") == 1,
        "the untampered pinned connector did not dispatch exactly once",
    )
