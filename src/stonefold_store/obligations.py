"""In-memory reference obligation-registry adapter (docs/06 §5b, v0.6 CS-034).

Implements the four-operation contract behind a declared obligation registry:
``query`` filters the held records by the gateway's typed selector;
``reserve``/``consume``/``release`` are idempotent per (obligation ref,
intent id). This is the reference/testing implementation — a real deployment
registers an adapter over its ERP/EMR. The reservation lifecycle (who calls
reserve/consume/release, and when) is CS-035, wired in the staging/dispatch/
settle paths; the state machine below is the substrate it drives.

Like every store in this package, losing the real backing system fails the
gate **closed** (the gate wraps ``query`` and maps an exception to RFC §10
``failureMode``, with the irreversible floor) — this in-memory form never
raises on its own.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from stonefold_core.obligation import (
    MISSING,
    ConsumeOutcome,
    ConsumeResult,
    EqConstraint,
    Obligation,
    ReleaseOutcome,
    ReserveOutcome,
    Selector,
    lookup_field,
    values_equal,
)


@dataclass
class _LineState:
    """Reservation/consumption bookkeeping for one obligation ref."""

    reserved_by: str | None = None
    consumed_by: str | None = None
    receipt: str | None = None


class InMemoryObligationRegistry:
    """Satisfies ``stonefold_core.obligation.ObligationRegistry``."""

    def __init__(self, records: Mapping[str, Mapping[str, Any]] | None = None) -> None:
        self._records: dict[str, dict[str, Any]] = {
            ref: dict(fields) for ref, fields in (records or {}).items()
        }
        self._state: dict[str, _LineState] = {}

    # --- record management (test/demo surface, not part of the contract) ---
    def add(self, ref: str, fields: Mapping[str, Any]) -> None:
        self._records[ref] = dict(fields)

    def remove(self, ref: str) -> None:
        self._records.pop(ref, None)

    def set_field(self, ref: str, path: str, value: Any) -> None:
        """Simulate the record system changing (a PO closed, a line posted)."""
        parts = path.split(".")
        node: dict[str, Any] = self._records[ref]
        for part in parts[:-1]:
            nxt = node.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                node[part] = nxt
            node = nxt
        node[parts[-1]] = value

    # --- the four-operation adapter contract (CS-034) ---
    def query(self, selector: Selector) -> list[Obligation]:
        """Typed records matching every ``EqConstraint``. A record missing a
        constrained field never matches (it cannot satisfy the comparison)."""
        out: list[Obligation] = []
        for ref, fields in self._records.items():
            if all(self._satisfies(fields, c) for c in selector):
                out.append(Obligation(ref=ref, fields=dict(fields)))
        return out

    @staticmethod
    def _satisfies(fields: Mapping[str, Any], c: EqConstraint) -> bool:
        value = lookup_field(fields, c.field)
        if value is MISSING or value is None:
            return False
        return values_equal(value, c.value)

    def reserve(self, ref: str, intent_id: str) -> ReserveOutcome:
        st = self._state.setdefault(ref, _LineState())
        if st.consumed_by is not None:
            return ReserveOutcome.ALREADY_CONSUMED
        if st.reserved_by is None or st.reserved_by == intent_id:
            st.reserved_by = intent_id  # idempotent per (ref, intent_id)
            return ReserveOutcome.RESERVED
        return ReserveOutcome.ALREADY_RESERVED

    def consume(self, ref: str, intent_id: str) -> ConsumeResult:
        st = self._state.setdefault(ref, _LineState())
        if st.consumed_by is not None:
            if st.consumed_by == intent_id:
                # a retry never double-consumes: same receipt, same outcome.
                return ConsumeResult(ConsumeOutcome.CONSUMED, receipt=st.receipt)
            return ConsumeResult(ConsumeOutcome.ALREADY_CONSUMED)
        st.consumed_by = intent_id
        st.reserved_by = None
        st.receipt = f"rcpt_{uuid.uuid4().hex}"
        return ConsumeResult(ConsumeOutcome.CONSUMED, receipt=st.receipt)

    def release(self, ref: str, intent_id: str) -> ReleaseOutcome:
        st = self._state.get(ref)
        if st is None or st.reserved_by != intent_id or st.consumed_by is not None:
            return ReleaseOutcome.NOT_HELD  # idempotent no-op
        st.reserved_by = None
        return ReleaseOutcome.RELEASED

    # --- introspection for tests ---
    def state_of(self, ref: str) -> _LineState:
        return self._state.setdefault(ref, _LineState())
