"""Stock precondition-check factories — the common shapes, pre-written.

Registered functions are the hand-written part of the trust surface (docs/06
§6 Bucket B). Most deployments need only a handful of shapes: "the target is
in state X", "the record is older than N hours", "the agent supplied field Y".
These factories cover them so no bespoke check code is written at all — and
each returned check is pure, deterministic, and **fails closed**: a missing
field, an unparsable value, or an absent injected clock returns ``False``
(deny), never raises. (A raised exception from a check is a *dependency
failure* and trips ``failureMode`` — reserve it for genuinely broken
dependencies, not absent data.)

Every factory's product passes ``stonefold_gates.conformance.check_precondition``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from stonefold_gates.base import GateContext, PreconditionCheck


def resource_state_in(field: str, *allowed: str) -> PreconditionCheck:
    """The resolved target's ``field`` (e.g. ``currentState``) is one of
    ``allowed``. Missing field ⇒ ``False`` (fail closed)."""

    allowed_set = frozenset(allowed)

    def check(ctx: GateContext) -> bool:
        value = ctx.env.resource.get(field)
        return value in allowed_set

    return check


def data_field_present(field: str) -> PreconditionCheck:
    """The agent supplied a non-empty ``data.field`` (e.g. an explanation)."""

    def check(ctx: GateContext) -> bool:
        value = ctx.resolved.data.get(field)
        return value is not None and value != ""

    return check


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def cooling_off_elapsed(field: str, min_age: timedelta) -> PreconditionCheck:
    """The target's ``field`` timestamp is at least ``min_age`` old — the
    new-payee cooling-off pattern (RFC §14.4). Reads the **injected** clock
    (``env.now``, invariant 1); missing clock, missing field, or an unparsable
    timestamp ⇒ ``False`` (fail closed)."""

    def check(ctx: GateContext) -> bool:
        now = ctx.env.now
        stamp = _as_datetime(ctx.env.resource.get(field))
        if now is None or stamp is None:
            return False
        return (now - stamp) >= min_age

    return check
