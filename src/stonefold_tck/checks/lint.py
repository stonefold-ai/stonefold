"""LINT profile — load-time validation refuses bad policies (RFC §13).

The gateway must refuse to run an invalid policy rather than degrade to a
permissive default. Warnings (A7b-style) require the driver to surface them
(CAP_LINT_WARNINGS); errors must always refuse the load.

Failure messages state the violation observed (what the implementation did
wrong), not the expectation — so a FAIL line in the report reads as the
defect it is.
"""

from __future__ import annotations

from stonefold_tck.checks import PROFILE_LINT, check, expect
from stonefold_tck.driver import CAP_LINT_WARNINGS, ConformanceDriver
from stonefold_tck.fixtures import (
    POLICY_INVALID_DUAL_QUORUM,
    POLICY_INVALID_OPEN_IRREVERSIBLE,
    POLICY_INVALID_STANDING_DENY,
    POLICY_INVALID_UNKNOWN_NAME,
    POLICY_MINIMAL_OBSERVE,
    POLICY_WARN_STAR_GRANT,
    REGISTRY_INVALID_HOLD_NO_CODES,
    TCK_POLICY,
    TCK_REGISTRY,
)


@check("A5", "the fixture registry/policy load cleanly", PROFILE_LINT)
def a5_fixtures_load(driver: ConformanceDriver) -> None:
    result = driver.load(TCK_REGISTRY, TCK_POLICY)
    expect(result.ok, f"the base fixtures failed to load: {list(result.errors)}")
    expect(not result.errors, f"the base fixtures produced errors: {list(result.errors)}")


@check("A4", "failureMode: open with an irreversible effect refuses to load", PROFILE_LINT)
def a4_open_on_irreversible(driver: ConformanceDriver) -> None:
    result = driver.load(TCK_REGISTRY, POLICY_INVALID_OPEN_IRREVERSIBLE)
    expect(not result.ok, "an open-on-irreversible policy loaded (must refuse, RFC §13.5)")


@check("A4b", "a policy naming an undeclared action refuses to load (deny included)", PROFILE_LINT)
def a4b_unknown_name(driver: ConformanceDriver) -> None:
    result = driver.load(TCK_REGISTRY, POLICY_INVALID_UNKNOWN_NAME)
    expect(not result.ok,
           "a policy denying an undeclared name loaded (must refuse, RFC §13.1, CS-016)")


@check("A6", "deny + standing.enables on the same action refuses to load", PROFILE_LINT)
def a6_standing_deny(driver: ConformanceDriver) -> None:
    result = driver.load(TCK_REGISTRY, POLICY_INVALID_STANDING_DENY)
    expect(not result.ok,
           "an unsatisfiable standing grant loaded (must refuse, RFC §13 rule 11)")


@check("A8", "dualAuthorization with quorum < 2 refuses to load", PROFILE_LINT)
def a8_dual_quorum(driver: ConformanceDriver) -> None:
    result = driver.load(TCK_REGISTRY, POLICY_INVALID_DUAL_QUORUM)
    expect(not result.ok,
           "a quorum-1 dualAuthorization loaded (must refuse, RFC §13 rule 13)")


@check("A9", "a hold-capable check declared without reasonCodes refuses to load", PROFILE_LINT)
def a9_hold_capable_needs_codes(driver: ConformanceDriver) -> None:
    # §13 rule 18 (CS-038): every hold such a check returned would be code-less
    # and resolve fail (CS-026 rule 2) — so the DECLARATION is the error.
    result = driver.load(REGISTRY_INVALID_HOLD_NO_CODES, POLICY_MINIMAL_OBSERVE)
    expect(not result.ok,
           "a holdCapable check with no reasonCodes loaded (must refuse, RFC §13 rule 18)")


@check("A7b", "a '*' grant loads but reports a warning", PROFILE_LINT, requires=[CAP_LINT_WARNINGS])
def a7b_star_grant_warns(driver: ConformanceDriver) -> None:
    result = driver.load(TCK_REGISTRY, POLICY_WARN_STAR_GRANT)
    expect(result.ok, "a '*' grant refused to load (it is a warning, not an error)")
    expect(bool(result.warnings), "a '*' grant surfaced no warning (RFC §13.6)")
