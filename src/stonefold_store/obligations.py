# SPDX-License-Identifier: Apache-2.0
"""In-memory reference obligation-registry adapter (docs/06 §5b, v0.6
CS-034/CS-035).

Implements the four-operation contract behind a declared obligation registry:
``query`` filters the held records by the gateway's typed selector;
``reserve``/``consume``/``release`` are idempotent per (obligation ref,
intent id). This is the reference/testing implementation — a real deployment
registers an adapter over its ERP/EMR.

Two behaviours a real adapter also owns, modelled here:

* **State visibility** (``state_path``): reserving/consuming a line flips its
  declared state field (e.g. ``line.state`` → ``reserved``/``consumed``, back
  to ``unconsumed`` on release), so a policy matching
  ``obligation.line.state == 'unconsumed'`` refuses a second intent at
  DECISION time — the resubmitted-invoice beat (RFC §14.4).
* **Reservation TTL** (CS-035 orphan recovery): reservations expire on the
  ADAPTER's own clock — a gateway crash between reserve and staging-commit
  must not lock a real order line forever. Expiry is lazy (checked on every
  operation); an expired-but-unclaimed reservation MAY be re-acquired by the
  same intent (the dispatch liveness probe), and releasing an expired
  reservation is the idempotent ``NOT_HELD`` no-op.

Like every store in this package, losing the real backing system fails the
gate **closed** (the gate wraps ``query`` and maps an exception to RFC §10
``failureMode``, with the irreversible floor) — this in-memory form never
raises on its own.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
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
    reserved_at: datetime | None = None
    consumed_by: str | None = None
    receipt: str | None = None


class InMemoryObligationRegistry:
    """Satisfies ``stonefold_core.obligation.ObligationRegistry``."""

    def __init__(
        self,
        records: Mapping[str, Mapping[str, Any]] | None = None,
        *,
        state_path: str | None = None,
        reservation_ttl_s: float | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._records: dict[str, dict[str, Any]] = {
            ref: dict(fields) for ref, fields in (records or {}).items()
        }
        self._state: dict[str, _LineState] = {}
        # the record field reserve/consume/release keep in sync (e.g.
        # "line.state" over values [unconsumed, reserved, consumed])
        self._state_path = state_path
        self._ttl_s = reservation_ttl_s
        # the ADAPTER's clock — deliberately separate from the gateway's
        # decision clock (F5.2: the gateway must tolerate skew between them)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

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

    def _mark(self, ref: str, state: str) -> None:
        if self._state_path is not None and ref in self._records:
            self.set_field(ref, self._state_path, state)

    def _expire_lazily(self, ref: str) -> None:
        """CS-035 orphan recovery: a reservation past the adapter's TTL is
        void — the line frees for the next intent (and its state field reads
        ``unconsumed`` again)."""
        st = self._state.get(ref)
        if (
            st is None
            or st.reserved_by is None
            or st.consumed_by is not None
            or self._ttl_s is None
            or st.reserved_at is None
        ):
            return
        if (self._clock() - st.reserved_at).total_seconds() >= self._ttl_s:
            st.reserved_by = None
            st.reserved_at = None
            self._mark(ref, "unconsumed")

    # --- the four-operation adapter contract (CS-034) ---
    def query(self, selector: Selector) -> list[Obligation]:
        """Typed records matching every ``EqConstraint``. A record missing a
        constrained field never matches (it cannot satisfy the comparison).
        Reserved/consumed lines are visible AS reserved/consumed (their state
        field moved), so an ``== 'unconsumed'`` clause refuses them here."""
        out: list[Obligation] = []
        for ref in self._records:
            self._expire_lazily(ref)
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
        self._expire_lazily(ref)
        st = self._state.setdefault(ref, _LineState())
        if st.consumed_by is not None:
            return ReserveOutcome.ALREADY_CONSUMED
        if st.reserved_by is None or st.reserved_by == intent_id:
            # idempotent per (ref, intent_id); re-reserving refreshes the TTL —
            # this is also the dispatch liveness probe (still held ⇒ RESERVED).
            st.reserved_by = intent_id
            st.reserved_at = self._clock()
            self._mark(ref, "reserved")
            return ReserveOutcome.RESERVED
        return ReserveOutcome.ALREADY_RESERVED

    def consume(self, ref: str, intent_id: str) -> ConsumeResult:
        self._expire_lazily(ref)
        st = self._state.setdefault(ref, _LineState())
        if st.consumed_by is not None:
            if st.consumed_by == intent_id:
                # a retry never double-consumes: same receipt, same outcome.
                return ConsumeResult(ConsumeOutcome.CONSUMED, receipt=st.receipt)
            return ConsumeResult(ConsumeOutcome.ALREADY_CONSUMED)
        if st.reserved_by is not None and st.reserved_by != intent_id:
            # the line is held by a different intent: this caller may not spend
            # it. ALREADY_CONSUMED is the contract's refusal signal (the caller
            # treats any non-CONSUMED as "not yours to spend").
            return ConsumeResult(ConsumeOutcome.ALREADY_CONSUMED)
        st.consumed_by = intent_id
        st.reserved_by = None
        st.reserved_at = None
        st.receipt = f"rcpt_{uuid.uuid4().hex}"
        self._mark(ref, "consumed")
        return ConsumeResult(ConsumeOutcome.CONSUMED, receipt=st.receipt)

    def release(self, ref: str, intent_id: str) -> ReleaseOutcome:
        self._expire_lazily(ref)
        st = self._state.get(ref)
        if st is None or st.reserved_by != intent_id or st.consumed_by is not None:
            return ReleaseOutcome.NOT_HELD  # idempotent no-op (incl. expired)
        st.reserved_by = None
        st.reserved_at = None
        self._mark(ref, "unconsumed")
        return ReleaseOutcome.RELEASED

    # --- introspection for tests ---
    def state_of(self, ref: str) -> _LineState:
        return self._state.setdefault(ref, _LineState())
