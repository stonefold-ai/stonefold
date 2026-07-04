"""M0 — registry resolution unit tests (design §2; RFC §12 step 1; plan M0 task 3).

Known names resolve to a typed ResolvedAction; unknown names raise
UnknownActionError (which the pipeline turns into a default DENY).
"""

from __future__ import annotations

import pytest

from stonefold_core import Kind, RawCall, Reversibility, UnknownActionError
from tests.conftest import min_registry


def test_resolve_known_observe() -> None:
    reg = min_registry()
    ra = reg.resolve(RawCall(resource="Customer", action="read"))
    assert ra.kind is Kind.OBSERVE
    assert ra.resource == "Customer"
    assert ra.action == "read"
    assert ra.connector == "sql"
    assert ra.attrs.resultSensitivity == "internal"


def test_resolve_carries_action_attributes() -> None:
    reg = min_registry()
    ra = reg.resolve(RawCall(resource="Email", action="sendEmail"))
    assert ra.kind is Kind.EFFECT
    assert ra.attrs.reversibility is Reversibility.COMPENSABLE
    assert ra.connector == "email"


def test_resolve_transition_carries_from_states() -> None:
    reg = min_registry()
    ra = reg.resolve(RawCall(resource="Order", action="confirm"))
    assert ra.kind is Kind.TRANSITION
    assert ra.from_states == ("pending_confirmation",)


def test_resolve_preserves_supplied_data() -> None:
    reg = min_registry()
    ra = reg.resolve(
        RawCall(resource="Email", action="sendEmail", data={"to": "a@b.test"})
    )
    assert ra.data == {"to": "a@b.test"}


def test_unknown_resource_raises() -> None:
    reg = min_registry()
    with pytest.raises(UnknownActionError):
        reg.resolve(RawCall(resource="Nope", action="read"))


def test_unknown_action_raises() -> None:
    reg = min_registry()
    with pytest.raises(UnknownActionError):
        reg.resolve(RawCall(resource="Customer", action="frobnicate"))


def test_missing_action_name_raises() -> None:
    reg = min_registry()
    with pytest.raises(UnknownActionError):
        reg.resolve(RawCall(resource="Customer", action=None))


def test_registry_introspection_helpers() -> None:
    reg = min_registry()
    assert reg.has_scope_predicate("assignedToCurrentUser")
    assert not reg.has_scope_predicate("nope")
    assert reg.has_content_hook("dlp.basic")
    assert reg.has_named_set("corporate-domains")
    assert reg.named_set("corporate-domains") == ("acme.example", "acme.test")
    assert reg.has_sink("careTeam")
    assert set(reg.actions_of_kind("Customer", Kind.OBSERVE)) == {
        "read",
        "readSealed",
    }
