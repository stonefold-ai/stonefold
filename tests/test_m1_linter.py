"""M1 — the semantic linter (RFC §13). Acceptance A4 + per-check coverage."""

from __future__ import annotations

from typing import Any

import pytest

from acp_core import (
    PolicyError,
    Severity,
    load_policy,
    load_registry,
    validate_only,
)
from tests.conftest import (
    full_registry,
    invalid_example_path,
    load_schema,
    load_yaml,
)


def _lint(policy: dict[str, Any]) -> Any:
    return validate_only(policy, full_registry())


def _codes(report: Any) -> set[str]:
    return {f.code for f in report.findings}


# --- A4: the INTENTIONALLY-INVALID fixture is rejected at load ---
def test_a4_invalid_policy_rejected() -> None:
    data = load_yaml(invalid_example_path())
    with pytest.raises(PolicyError) as exc:
        load_policy(data, full_registry(), schema=load_schema())
    report = exc.value.report
    codes = _codes(report)
    assert "13.5" in codes  # open-on-irreversible ERROR
    assert "13.6" in codes  # '*' grant WARN
    assert "13.4" in codes  # irreversible-unguarded WARN
    assert report.has_errors  # at least one ERROR ⇒ load fails, no fallback


# --- §13.1 unknown names ---
def test_unknown_resource_or_action_errors() -> None:
    report = _lint({"agent": "x", "allow": [{"observe": ["Nonexistent"]}]})
    assert any(f.code == "13.1" and f.severity is Severity.ERROR for f in report.findings)


def test_unknown_scope_predicate_errors() -> None:
    report = _lint(
        {
            "agent": "x",
            "allow": [{"observe": ["Customer"]}],
            "scope": {"Customer": "noSuchPredicate"},
        }
    )
    assert any("scope predicate" in f.message for f in report.findings)


def test_unknown_named_set_errors() -> None:
    report = _lint(
        {
            "agent": "x",
            "allow": [{"effect": ["sendEmail"]}],
            "gates": {"sendEmail": {"allowlist": {"field": "data.d", "set": "ghost-set"}}},
        }
    )
    assert any(f.code == "13.1" and "named set" in f.message for f in report.findings)


# --- §13.3 transition without from-states ---
def test_missing_from_states_errors() -> None:
    reg = load_registry(
        {
            "resources": {
                "Doc": {"actions": {"publish": {"kind": "transition"}}}  # no `from`
            }
        }
    )
    report = validate_only(
        {"agent": "x", "allow": [{"transition": {"Doc": ["publish"]}}]}, reg
    )
    assert any(f.code == "13.3" and f.severity is Severity.ERROR for f in report.findings)


# --- §13.5 open on irreversible ---
def test_open_on_irreversible_errors() -> None:
    report = _lint(
        {
            "agent": "x",
            "defaults": {"failureMode": "open"},
            "allow": [{"effect": ["wipeDisk"]}],
        }
    )
    assert any(f.code == "13.5" and f.severity is Severity.ERROR for f in report.findings)


def test_closed_on_irreversible_is_ok() -> None:
    report = _lint(
        {
            "agent": "x",
            "defaults": {"failureMode": "closed"},
            "allow": [{"effect": ["wipeDisk"]}],
        }
    )
    assert not any(f.code == "13.5" for f in report.findings)


# --- §13.6 star grant warns ---
def test_star_grant_warns() -> None:
    report = _lint({"agent": "x", "allow": [{"observe": "*"}]})
    assert any(f.code == "13.6" and f.severity is Severity.WARN for f in report.findings)


# --- §13.7 assess explainability required without requireExplanation ---
def test_assess_without_requireExplanation_errors() -> None:
    report = _lint({"agent": "x", "allow": [{"assess": ["triage"]}]})
    assert any(f.code == "13.7" and f.severity is Severity.ERROR for f in report.findings)


def test_assess_with_requireExplanation_ok() -> None:
    report = _lint(
        {
            "agent": "x",
            "allow": [{"assess": ["triage"]}],
            "gates": {"triage": {"requireExplanation": True}},
        }
    )
    assert not any(f.code == "13.7" for f in report.findings)


# --- §13.8 sensitive read without disclosure warns ---
def test_sensitive_read_without_disclosure_warns() -> None:
    report = _lint({"agent": "x", "allow": [{"observe": ["IntelRecord"]}]})
    assert any(f.code == "13.8" and f.severity is Severity.WARN for f in report.findings)


# --- §13.9 bad condition errors ---
def test_bad_condition_errors() -> None:
    report = _lint(
        {
            "agent": "x",
            "allow": [{"effect": ["sendEmail"]}],
            "gates": {"sendEmail": {"requireApproval": {"when": "bogus.path == 1"}}},
        }
    )
    assert any(f.code == "13.9" and f.severity is Severity.ERROR for f in report.findings)


def test_irreversible_unguarded_warns() -> None:
    # Disk.wipeDisk irreversible, allowed, no guard gate.
    report = _lint({"agent": "x", "allow": [{"effect": ["wipeDisk"]}]})
    assert any(f.code == "13.4" and f.severity is Severity.WARN for f in report.findings)
