"""LINT profile — load-time validation refuses bad policies (RFC §13).

The gateway must refuse to run an invalid policy rather than degrade to a
permissive default. Warnings (A7-style) require the driver to surface them
(CAP_LINT_WARNINGS); errors must always refuse the load.
"""

from __future__ import annotations

from stonefold_tck.checks import PROFILE_LINT, check, expect
from stonefold_tck.driver import CAP_LINT_WARNINGS, ConformanceDriver
from stonefold_tck.fixtures import (
    POLICY_INVALID_DUAL_QUORUM,
    POLICY_INVALID_OPEN_IRREVERSIBLE,
    POLICY_INVALID_STANDING_DENY,
    POLICY_INVALID_UNKNOWN_NAME,
    POLICY_WARN_STAR_GRANT,
    TCK_POLICY,
    TCK_REGISTRY,
)


@check("A5", "the fixture registry/policy load cleanly", PROFILE_LINT)
def a5_fixtures_load(driver: ConformanceDriver) -> None:
    result = driver.load(TCK_REGISTRY, TCK_POLICY)
    expect(result.ok, f"base fixtures must load: {list(result.errors)}")
    expect(not result.errors, f"base fixtures must produce no errors: {list(result.errors)}")


@check("A4", "failureMode: open with an irreversible effect refuses to load", PROFILE_LINT)
def a4_open_on_irreversible(driver: ConformanceDriver) -> None:
    result = driver.load(TCK_REGISTRY, POLICY_INVALID_OPEN_IRREVERSIBLE)
    expect(not result.ok, "open-on-irreversible policy must be refused (RFC §13.5)")


@check("A4b", "a policy naming an undeclared action refuses to load (deny included)", PROFILE_LINT)
def a4b_unknown_name(driver: ConformanceDriver) -> None:
    result = driver.load(TCK_REGISTRY, POLICY_INVALID_UNKNOWN_NAME)
    expect(not result.ok, "undeclared name (in deny) must be refused (RFC §13.1, CS-016)")


@check("A6", "deny + standing.enables on the same action refuses to load", PROFILE_LINT)
def a6_standing_deny(driver: ConformanceDriver) -> None:
    result = driver.load(TCK_REGISTRY, POLICY_INVALID_STANDING_DENY)
    expect(not result.ok, "unsatisfiable standing grant must be refused (RFC §13 rule 11)")


@check("A8", "dualAuthorization with quorum < 2 refuses to load", PROFILE_LINT)
def a8_dual_quorum(driver: ConformanceDriver) -> None:
    result = driver.load(TCK_REGISTRY, POLICY_INVALID_DUAL_QUORUM)
    expect(not result.ok, "quorum 1 dual-authorization must be refused (RFC §13 rule 13)")


@check("A7", "a '*' grant loads but reports a warning", PROFILE_LINT, requires=[CAP_LINT_WARNINGS])
def a7_star_grant_warns(driver: ConformanceDriver) -> None:
    result = driver.load(TCK_REGISTRY, POLICY_WARN_STAR_GRANT)
    expect(result.ok, "'*' grant is a warning, not an error — the policy loads")
    expect(bool(result.warnings), "'*' grant must surface a warning (RFC §13.6)")
