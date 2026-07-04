"""Run the check catalogue against a driver and report conformance.

Certification rule (docs/12 §4): a profile is **certified** only when every one
of its checks PASSED. A check skipped for a missing capability leaves the
profile "not certified — not attempted in full"; a skip is never a pass.
"""

from __future__ import annotations

import traceback
from collections.abc import Sequence
from dataclasses import dataclass

from stonefold_tck.checks import ALL_PROFILES, Check, ConformanceFailure, all_checks
from stonefold_tck.driver import ConformanceDriver

PASS = "pass"
FAIL = "fail"
SKIP = "skip"


@dataclass(frozen=True)
class CheckResult:
    check_id: str
    title: str
    profile: str
    status: str  # pass | fail | skip
    detail: str = ""


@dataclass(frozen=True)
class ConformanceReport:
    implementation: str
    results: tuple[CheckResult, ...]

    def by_profile(self, profile: str) -> tuple[CheckResult, ...]:
        return tuple(r for r in self.results if r.profile == profile)

    def certified_profiles(self) -> tuple[str, ...]:
        out: list[str] = []
        for profile in ALL_PROFILES:
            results = self.by_profile(profile)
            if results and all(r.status == PASS for r in results):
                out.append(profile)
        return tuple(out)

    @property
    def failures(self) -> tuple[CheckResult, ...]:
        return tuple(r for r in self.results if r.status == FAIL)

    def render(self) -> str:
        lines = [f"Stonefold TCK conformance report -- implementation: {self.implementation}"]
        for profile in ALL_PROFILES:
            results = self.by_profile(profile)
            if not results:
                continue
            passed = sum(1 for r in results if r.status == PASS)
            failed = sum(1 for r in results if r.status == FAIL)
            skipped = sum(1 for r in results if r.status == SKIP)
            verdict = "CERTIFIED" if failed == 0 and skipped == 0 else (
                "FAILED" if failed else "INCOMPLETE (skips)"
            )
            lines.append(f"\n[{profile}] {verdict} -- {passed} pass, {failed} fail, {skipped} skip")
            for r in results:
                mark = {"pass": "ok  ", "fail": "FAIL", "skip": "skip"}[r.status]
                line = f"  {mark} {r.check_id:<4} {r.title}"
                if r.detail:
                    line += f" -- {r.detail}"
                lines.append(line)
        certified = ", ".join(self.certified_profiles()) or "none"
        lines.append(f"\nCertified profiles: {certified}")
        return "\n".join(lines)


def run_conformance(
    driver: ConformanceDriver,
    *,
    implementation: str = "implementation-under-test",
    profiles: Sequence[str] | None = None,
) -> ConformanceReport:
    """Run every applicable check. ``load`` is called by each check, so state
    never leaks between them; a missing capability yields SKIP."""
    wanted = set(profiles) if profiles is not None else set(ALL_PROFILES)
    caps = driver.capabilities()
    results: list[CheckResult] = []
    for chk in all_checks():
        if chk.profile not in wanted:
            continue
        missing = chk.requires - caps
        if missing:
            results.append(
                CheckResult(chk.id, chk.title, chk.profile, SKIP,
                            f"driver lacks capabilities: {', '.join(sorted(missing))}")
            )
            continue
        results.append(_run_one(chk, driver))
    return ConformanceReport(implementation=implementation, results=tuple(results))


def _run_one(chk: Check, driver: ConformanceDriver) -> CheckResult:
    try:
        chk.fn(driver)
    except ConformanceFailure as failure:
        return CheckResult(chk.id, chk.title, chk.profile, FAIL, str(failure))
    except Exception:  # an unexpected crash is a failure, with the trace
        return CheckResult(chk.id, chk.title, chk.profile, FAIL,
                           "unexpected error:\n" + traceback.format_exc(limit=6))
    return CheckResult(chk.id, chk.title, chk.profile, PASS)
