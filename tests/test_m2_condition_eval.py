"""M2 — the condition engine *evaluator* (RFC §8, design §10).

Tree-walk over the parsed AST; no eval/exec. The critical safety property
(design §10, review note) is **fail-closed on a runtime resolution error** — a
missing path raises, it does not silently evaluate to ``False`` (acceptance C8 at
the evaluator level).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from acp_core import (
    ConditionRuntimeError,
    EvalContext,
    MissingValueError,
    evaluate_str,
    make_window,
)


def ctx(**ns: dict[str, object]) -> EvalContext:
    base: dict[str, dict[str, object]] = {
        "action": {},
        "data": {},
        "resource": {},
        "actor": {},
        "context": {},
    }
    base.update(ns)
    return EvalContext(
        namespaces=base, functions={"window": make_window, "now": lambda: None}
    )


def test_string_equality_with_implicit_literal() -> None:
    c = ctx(action={"reversibility": "irreversible"})
    assert evaluate_str("action.reversibility == irreversible", c) is True
    assert evaluate_str("action.reversibility == reversible", c) is False


def test_numeric_comparisons() -> None:
    c = ctx(data={"amount": 10001})
    assert evaluate_str("data.amount > 10000", c) is True
    assert evaluate_str("data.amount <= 10000", c) is False
    assert evaluate_str("data.amount == 10001", c) is True


def test_boolean_connectives() -> None:
    c = ctx(data={"a": 1, "b": 5})
    assert evaluate_str("data.a == 1 and data.b > 3", c) is True
    assert evaluate_str("data.a == 2 or data.b > 3", c) is True
    assert evaluate_str("not data.a == 2", c) is True
    assert evaluate_str("data.a == 1 and data.b < 3", c) is False


def test_in_and_not_in_list() -> None:
    c = ctx(data={"country": "KP"})
    assert evaluate_str("data.country in [KP, IR, SY]", c) is True
    assert evaluate_str("data.country not in [US, GB]", c) is True
    assert evaluate_str("data.country in [US, GB]", c) is False


def test_exists_does_not_raise_on_missing() -> None:
    assert evaluate_str("exists resource.foo", ctx(resource={})) is False
    assert evaluate_str("exists resource.foo", ctx(resource={"foo": 1})) is True


def test_missing_path_is_fail_closed_not_false() -> None:
    # C8 (evaluator level): a missing path RAISES — it is not silently 'false'.
    with pytest.raises(MissingValueError):
        evaluate_str("resource.foo == 1", ctx(resource={}))


def test_uncomparable_values_raise() -> None:
    with pytest.raises(ConditionRuntimeError):
        evaluate_str("data.x > 3", ctx(data={"x": "not-a-number"}))


def test_matches_regex() -> None:
    c = ctx(data={"email": "a@acme.example"})
    assert evaluate_str("data.email matches 'acme'", c) is True
    assert evaluate_str("data.email matches 'evil'", c) is False


def test_window_function_membership() -> None:
    inside = ctx(context={"time": "10:30"})
    outside = ctx(context={"time": "19:30"})
    assert evaluate_str("context.time in window('08:00-18:00')", inside) is True
    assert evaluate_str("context.time in window('08:00-18:00')", outside) is False


def test_window_membership_with_datetime() -> None:
    c = ctx(context={"time": datetime(2026, 6, 28, 9, 15, tzinfo=timezone.utc)})
    assert evaluate_str("context.time in window('08:00-18:00')", c) is True
