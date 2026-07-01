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


def test_deny_of_undeclared_name_errors() -> None:
    # v0.3 CS-016: rule 13.1 applies to deny too — "you deny things that exist".
    # A deny of an unknown name is a no-op (default-deny already refuses it) and
    # almost always a typo; it must not lint clean.
    report = _lint(
        {
            "agent": "x",
            "allow": [{"observe": ["Customer"]}],
            "deny": [{"effect": ["dropAllTables"]}],
        }
    )
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


# --- §13.10 (CS-008) compensable MUST declare a resolvable compensation ---
def test_compensable_without_compensation_errors() -> None:
    reg = load_registry(
        {"resources": {"Doc": {"actions": {
            "send": {"kind": "effect", "reversibility": "compensable"},  # no compensation
        }}}}
    )
    report = validate_only({"agent": "x", "allow": [{"effect": ["send"]}]}, reg)
    assert any(f.code == "13.10" and f.severity is Severity.ERROR for f in report.findings)


def test_compensable_with_compensation_ok() -> None:
    reg = load_registry(
        {"resources": {"Doc": {"actions": {
            "send": {"kind": "effect", "reversibility": "compensable",
                     "compensation": {"resource": "Doc", "action": "unsend"}},
            "unsend": {"kind": "effect", "reversibility": "irreversible"},
        }}}}
    )
    report = validate_only({"agent": "x", "allow": [{"effect": ["send"]}]}, reg)
    assert not any(f.code == "13.10" for f in report.findings)


def test_dangling_compensation_errors() -> None:
    reg = load_registry(
        {"resources": {"Doc": {"actions": {
            "send": {"kind": "effect", "reversibility": "compensable",
                     "compensation": {"resource": "Doc", "action": "ghost"}},  # not declared
        }}}}
    )
    report = validate_only({"agent": "x", "allow": [{"effect": ["send"]}]}, reg)
    assert any(f.code == "13.10" and "not in the registry" in f.message
               for f in report.findings)


def test_payments_pay_declares_refund_compensation() -> None:
    # the shipped registry's pay is compensable and now declares its undo → no 13.10
    report = validate_only({"agent": "x", "allow": [{"effect": ["pay"]}]}, full_registry())
    assert not any(f.code == "13.10" for f in report.findings)


# --- §13.11 (v0.3 CS-010, acceptance A6): standing cannot re-enable a deny ---
def test_standing_deny_conflict_errors() -> None:
    report = _lint(
        {
            "agent": "x",
            "allow": [{"observe": ["Track"]}],
            "deny": [{"effect": ["engage"]}],
            "standing": [
                {
                    "name": "weapons-free",
                    "when": "context.roeState == 'weapons_free'",
                    "enables": {"effect": ["engage"]},
                }
            ],
        }
    )
    assert any(
        f.code == "13.11" and f.severity is Severity.ERROR for f in report.findings
    )


def test_standing_without_deny_is_ok() -> None:
    report = _lint(
        {
            "agent": "x",
            "allow": [{"observe": ["Track"]}],
            "standing": [
                {
                    "name": "weapons-free",
                    "when": "context.roeState == 'weapons_free'",
                    "enables": {"effect": ["engage"]},
                }
            ],
        }
    )
    assert "13.11" not in _codes(report)


def test_standing_conflicts_with_map_form_deny() -> None:
    report = _lint(
        {
            "agent": "x",
            "allow": [{"observe": ["Order"]}],
            "deny": [{"transition": {"Order": ["cancel"]}}],
            "standing": [
                {
                    "name": "night-shift",
                    "when": "context.roeState == 'x'",
                    "enables": {"transition": {"Order": ["cancel"]}},
                }
            ],
        }
    )
    assert any(
        f.code == "13.11" and f.severity is Severity.ERROR for f in report.findings
    )


# --- §13.12 (v0.3 CS-012, acceptance A7): ambiguous bare-name allow warns ---
def test_ambiguous_bare_name_allow_warns() -> None:
    # the shipped registry declares an effect `exportData` on both Customer and Export
    report = _lint({"agent": "x", "allow": [{"effect": ["exportData"]}]})
    assert any(
        f.code == "13.12" and f.severity is Severity.WARN for f in report.findings
    )


def test_map_form_grant_is_unambiguous() -> None:
    report = _lint({"agent": "x", "allow": [{"effect": {"Export": ["exportData"]}}]})
    assert "13.12" not in _codes(report)


def test_unique_bare_name_allow_is_ok() -> None:
    report = _lint({"agent": "x", "allow": [{"effect": ["pay"]}]})
    assert "13.12" not in _codes(report)


# --- §13.13 (v0.3 CS-014, acceptance A8): dualAuthorization quorum < 2 ---
def test_dual_authorization_quorum_below_two_errors() -> None:
    report = _lint(
        {
            "agent": "x",
            "allow": [{"effect": ["pay"]}],
            "gates": {
                "pay": {"dualAuthorization": {"quorum": 1, "approvers": "role:treasury"}}
            },
        }
    )
    assert any(
        f.code == "13.13" and f.severity is Severity.ERROR for f in report.findings
    )


def test_dual_authorization_default_quorum_ok() -> None:
    report = _lint(
        {
            "agent": "x",
            "allow": [{"effect": ["pay"]}],
            "gates": {"pay": {"dualAuthorization": {"approvers": "role:treasury"}}},
        }
    )
    assert "13.13" not in _codes(report)
