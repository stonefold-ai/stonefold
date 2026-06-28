"""M1 — `extends` composition (RFC §3.2): deny in a fragment cannot be widened.

Merge rules: allow/deny/gates/scope unioned; deny always wins; composition MUST
NOT widen a permission a fragment denied (design §4).
"""

from __future__ import annotations

from typing import Any

from acp_core import (
    Actor,
    Decision,
    InMemoryAuditSink,
    RawCall,
    Session,
    enforce,
    load_policy,
)
from tests.conftest import full_registry, load_schema


def _decide(policy: Any, resource: str, action: str) -> Decision:
    audit = InMemoryAuditSink()
    return enforce(
        RawCall(resource=resource, action=action),
        Actor(id="alice"),
        Session(id="s"),
        registry=full_registry(),
        audit=audit,
        policy=policy,
    ).decision


def test_extends_unions_allow() -> None:
    fragment = {"agent": "frag", "allow": [{"observe": ["Customer"]}]}
    child = {
        "agent": "child",
        "extends": ["base"],
        "allow": [{"effect": ["sendEmail"]}],
    }
    compiled = load_policy(
        child, full_registry(), schema=load_schema(), fragments={"base": fragment}
    )
    # both the fragment's and the child's grants are present
    assert _decide(compiled, "Customer", "read") is Decision.ALLOW
    assert _decide(compiled, "Email", "sendEmail") is Decision.ALLOW


def test_extends_deny_cannot_be_widened() -> None:
    # The fragment denies sendEmail; the child tries to allow it. Deny wins.
    fragment = {
        "agent": "frag",
        "allow": [{"observe": ["Customer"]}],
        "deny": [{"effect": ["sendEmail"]}],
    }
    child = {
        "agent": "child",
        "extends": ["base"],
        "allow": [{"effect": ["sendEmail"]}],
    }
    compiled = load_policy(
        child, full_registry(), schema=load_schema(), fragments={"base": fragment}
    )
    audit = InMemoryAuditSink()
    result = enforce(
        RawCall(resource="Email", action="sendEmail"),
        Actor(id="alice"),
        Session(id="s"),
        registry=full_registry(),
        audit=audit,
        policy=compiled,
    )
    assert result.decision is Decision.DENY
    assert result.rule == "deny-rule"
