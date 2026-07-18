# SPDX-License-Identifier: Apache-2.0
"""Typed dependency results and the fail-closed resolution (RFC §10, design §12).

A *dependency failure* is concretely the registry, a scope resolver, a
``contentCheck`` hook, the kill store, or the outbox/audit DB being unavailable or
erroring (design §12). The rule (invariant 7): **never let such an exception
bubble into an implicit allow.** Each external call is wrapped in a typed
``DependencyResult`` (``Ok`` | ``Unavailable``) and the branch is taken from the
policy's ``failureMode`` — ``closed`` (the default) denies/halts, ``open`` allows
for low-stakes scopes — with one hard floor: an **irreversible effect always
fails closed**, regardless of ``failureMode`` (a control you cannot evaluate must
not be assumed absent).

This module is pure (no I/O, no framework) — part of the trust kernel.

STONEFOLD-AMBIGUITY (RFC §10): the RFC permits ``failureMode`` to be overridden *per
kind/action*; the pinned policy schema (``schema/stele.schema.json``) only carries
``defaults.failureMode``, so this POC resolves the mode at the defaults
granularity. Per-action override is a schema extension deferred past the concept
deliverable; the irreversible floor below already provides the most important
per-action behaviour (the §13.5 linter check enforces the open-on-irreversible
guard at load time).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar, Union

from stonefold_core.enums import Kind, Reversibility
from stonefold_core.models import ResolvedAction
from stonefold_core.policy import FailureMode

T = TypeVar("T")


@dataclass(frozen=True)
class Ok(Generic[T]):
    """A dependency call that succeeded, carrying its value."""

    value: T


@dataclass(frozen=True)
class Unavailable:
    """A dependency call that failed — the branch point for ``failureMode``."""

    reason: str
    error: BaseException | None = None


# The result of a guarded dependency call.
DependencyResult = Union[Ok[T], Unavailable]


def guard(thunk: Callable[[], T], *, reason: str) -> DependencyResult[T]:
    """Run an external dependency call, capturing *any* exception as
    ``Unavailable`` rather than letting it propagate into an implicit allow
    (design §12: "wrap each external dependency call in a typed result")."""
    try:
        return Ok(thunk())
    except Exception as exc:  # a dependency failure is opaque to the pipeline
        return Unavailable(reason=reason, error=exc)


def should_fail_closed(
    resolved: ResolvedAction | None, failure_mode: FailureMode
) -> bool:
    """Decide whether a dependency failure denies/halts (``True``) or is allowed
    through (``False``), per RFC §10.

    The irreversible floor wins over ``failureMode``: an irreversible effect is
    denied/halted on *any* dependency failure (design §8.9/§12, invariant 7).
    Otherwise ``closed`` ⇒ fail closed, ``open`` ⇒ allow through.
    """
    if (
        resolved is not None
        and resolved.kind is Kind.EFFECT
        and resolved.attrs.reversibility is Reversibility.IRREVERSIBLE
    ):
        return True
    return failure_mode is FailureMode.CLOSED
