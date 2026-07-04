"""M3 — the scope abstraction (RFC §6.3, design §5). Pure unit tests: predicate
membership, the realised SQL/HTTP forms, the empty-scope rule, and resolution
from a policy's ``scope`` block."""

from __future__ import annotations

from stonefold_core import (
    Actor,
    AttributeScope,
    ScopeResolver,
    default_scope_registry,
    load_policy,
)
from tests.conftest import full_registry, load_schema


def test_attribute_scope_matches_on_actor_id() -> None:
    scope = AttributeScope("assignedToCurrentUser", "owner_id", "id")
    alice = Actor(id="alice")
    assert scope.matches({"owner_id": "alice"}, alice) is True
    assert scope.matches({"owner_id": "bob"}, alice) is False


def test_attribute_scope_matches_on_claim() -> None:
    scope = AttributeScope("tenantOf", "tenant_id", "tenant")
    actor = Actor(id="a", claims={"tenant": "T1"})
    assert scope.matches({"tenant_id": "T1"}, actor) is True
    assert scope.matches({"tenant_id": "T2"}, actor) is False


def test_sql_where_uses_psycopg_placeholder() -> None:
    scope = AttributeScope("assignedToCurrentUser", "owner_id", "id")
    clause, params = scope.sql_where(Actor(id="alice"))
    assert clause == "owner_id = %(scope_owner_id)s"
    assert params == {"scope_owner_id": "alice"}


def test_query_param_for_http() -> None:
    scope = AttributeScope("tenantOf", "tenant_id", "tenant")
    name, value = scope.query_param(Actor(id="a", claims={"tenant": "T1"}))
    assert (name, value) == ("tenant_id", "T1")


def test_empty_scope_selects_nothing_never_widens() -> None:
    # actor missing the claim ⇒ empty set: matches nothing, SQL is 1=0 (RFC §6.3).
    scope = AttributeScope("tenantOf", "tenant_id", "tenant")
    actor = Actor(id="a")  # no tenant claim
    assert scope.is_empty(actor) is True
    assert scope.matches({"tenant_id": "T1"}, actor) is False
    clause, params = scope.sql_where(actor)
    assert clause == "1 = 0"
    assert params == {}


def test_scope_resolver_reads_policy_block() -> None:
    reg = full_registry()
    doc = {
        "agent": "x",
        "allow": [{"observe": ["Customer"]}],
        "scope": {"Customer": "assignedToCurrentUser"},
    }
    policy = load_policy(doc, reg, schema=load_schema())
    resolver = ScopeResolver(policy.policy.scope, default_scope_registry())
    pred = resolver.scope_for("Customer")
    assert pred is not None and pred.name == "assignedToCurrentUser"
    assert resolver.scope_for("Order") is None  # no scope declared


def test_scope_resolver_strips_call_form() -> None:
    reg = full_registry()
    doc = {
        "agent": "x",
        "allow": [{"observe": ["Matter"]}],
        "scope": {"Matter": "clientOf(actor)"},
    }
    policy = load_policy(doc, reg, schema=load_schema())
    resolver = ScopeResolver(policy.policy.scope, default_scope_registry())
    pred = resolver.scope_for("Matter")
    assert pred is not None and pred.name == "clientOf"
