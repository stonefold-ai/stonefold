"""stonefold_gates.conformance + stonefold_gates.stock — the registered-function kit.

Registered functions (precondition checks, content hooks, scope predicates)
are hand-written, security-critical code (docs/06 §6 Bucket B). The kit is the
test-time harness a deployment runs over each one before registering it:
determinism, totality over golden cases, non-mutation of inputs, and golden
expectations. The stock factories cover the common shapes so most deployments
write no bespoke check code at all.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

import pytest

from stonefold_core import Actor
from stonefold_core.gating import RequestEnv
from stonefold_core.scope import AttributeScope
from stonefold_gates.base import GateContext
from stonefold_gates.conformance import (
    ConformanceIssue,
    assert_conformant,
    check_content_hook,
    check_precondition,
    check_scope_predicate,
)
from stonefold_gates.stock import cooling_off_elapsed, data_field_present, resource_state_in
from tests.conftest import gate_ctx

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


def _ctx(
    state: str | None = "draft",
    *,
    data: dict[str, Any] | None = None,
    resource_extra: dict[str, Any] | None = None,
    now: datetime | None = NOW,
) -> GateContext:
    resource: dict[str, Any] = dict(resource_extra or {})
    if state is not None:
        resource["currentState"] = state
    return gate_ctx("Order", "sign", data=data, env=RequestEnv(resource=resource, now=now))


# --------------------------------------------------------------------------
# stock factories (fail-closed: missing input ⇒ False, never an exception)
# --------------------------------------------------------------------------
def test_stock_resource_state_in() -> None:
    check = resource_state_in("currentState", "draft", "pending")
    assert check(_ctx("draft")) is True
    assert check(_ctx("signed")) is False
    assert check(_ctx(None)) is False  # missing field fails closed


def test_stock_cooling_off_elapsed() -> None:
    check = cooling_off_elapsed("createdAt", timedelta(hours=24))
    old = (NOW - timedelta(hours=25)).isoformat()
    new = (NOW - timedelta(hours=1)).isoformat()
    assert check(_ctx(resource_extra={"createdAt": old})) is True
    assert check(_ctx(resource_extra={"createdAt": new})) is False
    assert check(_ctx()) is False  # missing field fails closed
    # missing injected clock fails closed too (determinism, invariant 1)
    assert check(_ctx(resource_extra={"createdAt": old}, now=None)) is False


def test_stock_cooling_off_accepts_datetime_values() -> None:
    check = cooling_off_elapsed("createdAt", timedelta(hours=24))
    assert check(_ctx(resource_extra={"createdAt": NOW - timedelta(days=2)})) is True


def test_stock_data_field_present() -> None:
    check = data_field_present("explanation")
    assert check(_ctx(data={"explanation": "RR 30"})) is True
    assert check(_ctx(data={"explanation": ""})) is False  # empty is absent
    assert check(_ctx()) is False


# --------------------------------------------------------------------------
# the kit: precondition checks
# --------------------------------------------------------------------------
def test_kit_passes_a_conformant_check() -> None:
    check = resource_state_in("currentState", "draft")
    issues = check_precondition(
        "stateDraft", check, cases=[(_ctx("draft"), True), (_ctx("signed"), False)]
    )
    assert issues == []


def test_kit_catches_nondeterminism() -> None:
    flips: list[bool] = []

    def flaky(ctx: GateContext) -> bool:
        flips.append(True)
        return len(flips) % 2 == 0

    issues = check_precondition("flaky", flaky, cases=[(_ctx(), True)])
    assert any("determin" in i.problem.lower() for i in issues)


def test_kit_catches_exceptions() -> None:
    def broken(ctx: GateContext) -> bool:
        raise KeyError("boom")

    issues = check_precondition("broken", broken, cases=[(_ctx(), False)])
    assert any("raise" in i.problem.lower() for i in issues)


def test_kit_catches_golden_mismatch() -> None:
    def always_true(ctx: GateContext) -> bool:
        return True

    issues = check_precondition("alwaysTrue", always_true, cases=[(_ctx(), False)])
    assert any("expected" in i.problem.lower() for i in issues)


def test_kit_catches_input_mutation() -> None:
    def mutating(ctx: GateContext) -> bool:
        resource = ctx.env.resource
        if isinstance(resource, dict):
            resource["tampered"] = True
        return True

    issues = check_precondition("mutating", mutating, cases=[(_ctx(), True)])
    assert any("mutat" in i.problem.lower() for i in issues)


# --------------------------------------------------------------------------
# the kit: content hooks
# --------------------------------------------------------------------------
def test_kit_content_hook_conformant_and_mutating() -> None:
    def clean_hook(content: Mapping[str, Any]) -> bool:
        return "ssn" not in str(content.get("body", "")).lower()

    ok = check_content_hook(
        "clean", clean_hook, cases=[({"body": "hello"}, True), ({"body": "my SSN is"}, False)]
    )
    assert ok == []

    def dirty_hook(content: Mapping[str, Any]) -> bool:
        if isinstance(content, dict):
            content["seen"] = True
        return True

    issues = check_content_hook("dirty", dirty_hook, cases=[({"body": "x"}, True)])
    assert any("mutat" in i.problem.lower() for i in issues)


# --------------------------------------------------------------------------
# the kit: scope predicates
# --------------------------------------------------------------------------
def test_kit_scope_predicate_conformant() -> None:
    pred = AttributeScope("tenantOf", "tenant_id", "tenant")
    alice = Actor(id="alice", claims={"tenant": "t1"})
    ghost = Actor(id="ghost")  # resolves to an empty scope
    rows: list[Mapping[str, Any]] = [{"tenant_id": "t1"}, {"tenant_id": "t2"}]
    issues = check_scope_predicate("tenantOf", pred, actors=[alice, ghost], rows=rows)
    assert issues == []


def test_kit_scope_predicate_catches_instability() -> None:
    class Wobbly:
        name = "wobbly"
        _n = 0

        def matches(self, attrs: Mapping[str, Any], actor: Actor) -> bool:
            Wobbly._n += 1
            return Wobbly._n % 2 == 0

        def sql_where(self, actor: Actor) -> tuple[str, dict[str, Any]]:
            return "1 = 1", {}

        def query_param(self, actor: Actor) -> tuple[str, Any]:
            return "x", 1

    issues = check_scope_predicate(
        "wobbly", Wobbly(), actors=[Actor(id="a")], rows=[{"tenant_id": "t1"}]
    )
    assert any("determin" in i.problem.lower() for i in issues)


# --------------------------------------------------------------------------
# assert_conformant
# --------------------------------------------------------------------------
def test_assert_conformant_raises_with_report() -> None:
    issues = [ConformanceIssue(subject="x", problem="not deterministic")]
    with pytest.raises(AssertionError, match="not deterministic"):
        assert_conformant(issues)
    assert_conformant([])  # no issues ⇒ no raise
