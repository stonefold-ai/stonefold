"""M5 — kill-switch core matching (design §8.2, RFC §9).

Unit-tests the pure ``stonefold_core.kill`` value types: scope matching for the four
scopes (GLOBAL / AGENT / SESSION / ACTION_CLASS), the optional §8 predicate, the
lift semantics, and the fail-closed behaviour when a predicate cannot be
evaluated. No I/O — this is all in the kernel.
"""

from __future__ import annotations

from typing import Any

from stonefold_core import Actor, RawCall, Session
from stonefold_core.kill import (
    KillOrder,
    KillScope,
    KillScopeKind,
    KillTarget,
    order_matches,
    scope_matches,
)
from stonefold_store.kill_memory import InMemoryKillStore
from tests.conftest import full_registry


def _target(resource: str, action: str, *, agent: str = "support", session: str = "s1",
            data: dict[str, Any] | None = None, actor: Actor | None = None) -> KillTarget:
    resolved = full_registry().resolve(RawCall(resource=resource, action=action, data=data or {}))
    return KillTarget.from_resolved(
        resolved, actor or Actor(id="alice"), Session(id=session), agent
    )


# --- scope matching ------------------------------------------------------
def test_global_scope_matches_anything() -> None:
    scope = KillScope.for_global()
    assert scope_matches(scope, _target("Email", "sendEmail"))
    assert scope_matches(scope, _target("Payment", "pay"))


def test_agent_scope_matches_only_that_agent() -> None:
    scope = KillScope.for_agent("support")
    assert scope_matches(scope, _target("Email", "sendEmail", agent="support"))
    assert not scope_matches(scope, _target("Email", "sendEmail", agent="pay"))


def test_session_scope_matches_only_that_session() -> None:
    scope = KillScope.for_session("s1")
    assert scope_matches(scope, _target("Email", "sendEmail", session="s1"))
    assert not scope_matches(scope, _target("Email", "sendEmail", session="s2"))


def test_action_class_matches_by_facets() -> None:
    # resource+action facet
    scope = KillScope.for_action_class(resource="Payment", action="pay")
    assert scope_matches(scope, _target("Payment", "pay"))
    assert not scope_matches(scope, _target("Payment", "refund"))
    assert not scope_matches(scope, _target("Email", "sendEmail"))


def test_action_class_kind_facet_is_a_wildcard_over_resources() -> None:
    from stonefold_core.enums import Kind

    scope = KillScope.for_action_class(kind=Kind.EFFECT)
    assert scope_matches(scope, _target("Email", "sendEmail"))  # an effect
    assert scope_matches(scope, _target("Payment", "pay"))  # an effect
    assert not scope_matches(scope, _target("Customer", "read"))  # an observe


def test_action_class_does_not_match_a_bare_top_level_target() -> None:
    # The top-of-pipeline pre-check builds a target with no kind/resource; an
    # ACTION_CLASS order must not match it (it is matched at step 5 instead).
    scope = KillScope.for_action_class(resource="Payment", action="pay")
    bare = KillTarget(agent="pay", session_id="s1")
    assert not scope_matches(scope, bare)


# --- predicates (optional §8 condition) ----------------------------------
def test_predicate_narrows_an_action_class_kill() -> None:
    order = _order(KillScope.for_action_class(resource="Payment", action="pay"),
                   predicate="data.amount > 1000")
    assert order_matches(order, _target("Payment", "pay", data={"amount": 5000}))
    assert not order_matches(order, _target("Payment", "pay", data={"amount": 10}))


def test_predicate_that_cannot_be_evaluated_fails_closed_to_kill() -> None:
    # A predicate over a missing path must HALT (kill wins on ambiguity), never
    # silently let the action through.
    order = _order(KillScope.for_global(), predicate="data.amount > 1000")
    assert order_matches(order, _target("Email", "sendEmail"))  # no data.amount ⇒ kill


# --- lifecycle -----------------------------------------------------------
def test_lifted_order_never_matches() -> None:
    store = InMemoryKillStore()
    order = store.issue(KillScope.for_session("s1"), issued_by="op")
    target = _target("Email", "sendEmail", session="s1")
    assert store.matches(target) is not None
    store.lift(order.id)
    assert store.matches(target) is None


def test_issue_advances_epoch() -> None:
    store = InMemoryKillStore()
    e0 = store.epoch()
    store.issue(KillScope.for_global(), issued_by="op")
    assert store.epoch() > e0


def _order(scope: KillScope, *, predicate: str | None = None) -> KillOrder:
    from datetime import datetime, timezone

    return KillOrder(
        id="kill_test", scope=scope, predicate=predicate, issued_by="op",
        issued_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
