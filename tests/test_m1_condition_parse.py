"""M1 — condition parser + §13.9 validation (RFC §8, design §10)."""

from __future__ import annotations

import pytest

from acp_core import ConditionError, parse_and_validate, parse_condition
from acp_core.condition import And, Compare, Exists, InExpr, Literal, Not, Or, Path


def test_simple_comparison() -> None:
    expr = parse_condition("action.reversibility == irreversible")
    assert isinstance(expr, Compare)
    assert isinstance(expr.left, Path)
    assert expr.left.parts == ("action", "reversibility")
    assert expr.op == "=="
    # bare RHS ident parses as a single-segment path (implicit string literal)
    assert isinstance(expr.right, Path)
    assert expr.right.parts == ("irreversible",)


def test_and_or_precedence() -> None:
    expr = parse_condition("data.amount > 1000 and data.amount <= 10000")
    assert isinstance(expr, And)
    expr2 = parse_condition("a.b == 1 or a.b == 2 and a.c == 3")
    # 'and' binds tighter than 'or'
    assert isinstance(expr2, Or)
    assert isinstance(expr2.right, And)


def test_not_and_exists() -> None:
    expr = parse_condition(
        "action.resultSensitivity == restricted and not exists context.breakGlass"
    )
    assert isinstance(expr, And)
    assert isinstance(expr.right, Not)
    assert isinstance(expr.right.expr, Exists)
    assert expr.right.expr.path.parts == ("context", "breakGlass")


def test_in_list() -> None:
    expr = parse_condition("data.country in ['KP', 'IR']")
    assert isinstance(expr, InExpr)
    assert not expr.negated
    assert isinstance(expr.right, Literal)


def test_not_in() -> None:
    expr = parse_condition("data.x not in [1, 2, 3]")
    assert isinstance(expr, InExpr)
    assert expr.negated


def test_parens() -> None:
    expr = parse_condition("(a.b == 1 or a.b == 2) and a.c == 3")
    assert isinstance(expr, And)
    assert isinstance(expr.left, Or)


def test_function_call_in_condition() -> None:
    expr = parse_condition("context.time in window('08:00-18:00')")
    assert isinstance(expr, InExpr)


def test_string_and_number_and_bool_literals() -> None:
    parse_condition("context.roeState == 'weapons_free'")
    parse_condition("data.amount > 50000")
    parse_condition("data.flag == true")


def test_validate_rejects_unknown_namespace() -> None:
    problems = parse_and_validate("foo.bar == 1")
    assert problems
    assert "namespace" in problems[0]


def test_validate_rejects_unknown_function() -> None:
    problems = parse_and_validate("data.x == bogus(1)")
    assert any("function" in p for p in problems)


def test_validate_accepts_known_namespaces_and_functions() -> None:
    assert parse_and_validate("action.kind == observe") == []
    assert parse_and_validate("context.sessionSpend > 10") == []
    assert parse_and_validate("context.time in window('08:00-18:00')") == []


def test_parse_error_surfaces() -> None:
    with pytest.raises(ConditionError):
        parse_condition("action.kind ==")


def test_parse_and_validate_reports_parse_failure() -> None:
    problems = parse_and_validate("== broken")
    assert problems and "cannot parse" in problems[0]
