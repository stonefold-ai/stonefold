"""Conformance kit for registered functions (docs/06 §6).

Precondition checks, content hooks, and scope predicates are the hand-written,
security-critical part of a deployment (Bucket B): the gateway guarantees
*when* they run, but their bodies are integrator code. This kit is the
test-time harness to run over each one **before registering it** — the same
way policies are linted before they load:

* **determinism** — the same input yields the same result, every time;
* **totality** — no exceptions over the golden cases (a runtime exception is a
  *dependency failure* and trips ``failureMode``; it must never be how a check
  expresses a verdict);
* **non-mutation** — the function reads its inputs, never writes them;
* **golden expectations** — the author states what the function must return
  for known inputs, and the kit holds it to that.

Authoring/test-time only — nothing here runs in the enforcement path.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from stonefold_core.models import Actor
from stonefold_core.scope import ScopePredicate
from stonefold_gates.base import GateContext, PreconditionCheck
from stonefold_gates.content import ContentHook

_DEFAULT_REPEATS = 3


@dataclass(frozen=True)
class ConformanceIssue:
    """One violation found by the kit."""

    subject: str  # "<name>[case N]"
    problem: str


def assert_conformant(issues: Sequence[ConformanceIssue]) -> None:
    """Raise ``AssertionError`` with a readable report if any issue was found
    (pytest-friendly: call it at the end of a deployment's conformance test)."""
    if issues:
        report = "\n".join(f"  {i.subject}: {i.problem}" for i in issues)
        raise AssertionError(f"registered function is not conformant:\n{report}")


def _run_repeated(
    subject: str,
    call: Any,
    expected: bool,
    issues: list[ConformanceIssue],
    repeats: int,
) -> None:
    """Run ``call()`` ``repeats`` times: no exception, stable, matches golden."""
    results: list[bool] = []
    for _ in range(repeats):
        try:
            results.append(bool(call()))
        except Exception as exc:  # totality: a verdict must not be an exception
            issues.append(
                ConformanceIssue(subject, f"raised {type(exc).__name__}: {exc} (a check "
                                          f"expresses its verdict as a bool; an exception is a dependency failure)")
            )
            return
    if len(set(results)) > 1:
        issues.append(ConformanceIssue(subject, f"not deterministic: {results} across {repeats} identical calls"))
        return
    if results[0] != expected:
        issues.append(ConformanceIssue(subject, f"returned {results[0]}, expected {expected} (golden case)"))


def _snapshot_ctx_inputs(ctx: GateContext) -> tuple[Any, Any, Any]:
    return (
        copy.deepcopy(dict(ctx.env.resource)),
        copy.deepcopy(dict(ctx.env.context)),
        copy.deepcopy(dict(ctx.resolved.data)),
    )


def check_precondition(
    name: str,
    fn: PreconditionCheck,
    cases: Sequence[tuple[GateContext, bool]],
    *,
    repeats: int = _DEFAULT_REPEATS,
) -> list[ConformanceIssue]:
    """Conformance-check a precondition check over golden ``(ctx, expected)`` cases."""
    issues: list[ConformanceIssue] = []
    for n, (ctx, expected) in enumerate(cases):
        subject = f"{name}[case {n}]"
        before = _snapshot_ctx_inputs(ctx)
        _run_repeated(subject, lambda c=ctx: fn(c), expected, issues, repeats)
        if _snapshot_ctx_inputs(ctx) != before:
            issues.append(ConformanceIssue(subject, "mutated its input context (checks must be read-only)"))
    return issues


def check_content_hook(
    name: str,
    hook: ContentHook,
    cases: Sequence[tuple[Mapping[str, Any], bool]],
    *,
    repeats: int = _DEFAULT_REPEATS,
) -> list[ConformanceIssue]:
    """Conformance-check a content hook over golden ``(content, expected)`` cases."""
    issues: list[ConformanceIssue] = []
    for n, (content, expected) in enumerate(cases):
        subject = f"{name}[case {n}]"
        before = copy.deepcopy(dict(content))
        _run_repeated(subject, lambda c=content: hook(c), expected, issues, repeats)
        if dict(content) != before:
            issues.append(ConformanceIssue(subject, "mutated its input content (hooks must be read-only)"))
    return issues


def check_scope_predicate(
    name: str,
    pred: ScopePredicate,
    *,
    actors: Sequence[Actor],
    rows: Sequence[Mapping[str, Any]],
    golden: Sequence[tuple[Mapping[str, Any], Actor, bool]] = (),
    repeats: int = _DEFAULT_REPEATS,
) -> list[ConformanceIssue]:
    """Conformance-check a scope predicate.

    For every actor × row: ``matches`` is deterministic, raises nothing, and
    leaves the row untouched; ``sql_where``/``query_param`` are stable per
    actor. Optional ``golden`` triples pin expected membership results.
    """
    issues: list[ConformanceIssue] = []
    for actor in actors:
        subject = f"{name}[actor {actor.id}]"
        for realise in ("sql_where", "query_param"):
            try:
                results = [getattr(pred, realise)(actor) for _ in range(repeats)]
            except Exception as exc:
                issues.append(ConformanceIssue(subject, f"{realise} raised {type(exc).__name__}: {exc}"))
                continue
            if any(r != results[0] for r in results[1:]):
                issues.append(ConformanceIssue(subject, f"{realise} not deterministic: {results}"))
        for m, row in enumerate(rows):
            row_subject = f"{name}[actor {actor.id}, row {m}]"
            before = copy.deepcopy(dict(row))
            try:
                outcomes = [pred.matches(row, actor) for _ in range(repeats)]
            except Exception as exc:
                issues.append(ConformanceIssue(row_subject, f"matches raised {type(exc).__name__}: {exc}"))
                continue
            if len(set(outcomes)) > 1:
                issues.append(
                    ConformanceIssue(row_subject, f"matches not deterministic: {outcomes} across {repeats} calls")
                )
            if dict(row) != before:
                issues.append(ConformanceIssue(row_subject, "mutated the row (predicates must be read-only)"))
    for n, (row, actor, expected) in enumerate(golden):
        subject = f"{name}[golden {n}]"
        _run_repeated(subject, lambda r=row, a=actor: pred.matches(r, a), expected, issues, repeats)
    return issues
