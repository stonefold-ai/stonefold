"""M5 — **E4** an in-flight cancellable connector call is aborted (design §8.5).

A long-running cancellable dispatch is in flight when a kill is issued. The
gateway keeps a registry of in-flight calls keyed by their cancellation handle;
issuing the kill invokes the matching handle, the connector aborts, and the row
settles to a terminal ``CANCELLED`` state — audited, never left dangling.
"""

from __future__ import annotations

import threading
from typing import Any

from stonefold_core import Actor, Connectors, InMemoryAuditSink, PendingState, RawCall
from stonefold_core.connector import ConnectorCancelled, ConnectorResult
from stonefold_core.kill import KillScope
from stonefold_core.models import ResolvedAction
from stonefold_core.scope import ScopePredicate
from stonefold_store import DispatchWorker, InMemoryOutboxStore
from stonefold_store.inflight import InFlightRegistry
from stonefold_store.kill_memory import InMemoryKillStore
from stonefold_gateway.kill_service import KillService
from tests.conftest import full_registry


class _BlockingConnector:
    """``dispatch`` blocks until ``cancel`` aborts it (a long HTTP call / job)."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self._abort = threading.Event()
        self.cancelled: list[str] = []

    def dispatch(self, action: ResolvedAction, actor: Actor, idempotency_key: str) -> ConnectorResult:
        self.started.set()
        if self._abort.wait(timeout=3.0):
            raise ConnectorCancelled(idempotency_key)
        return ConnectorResult(kind="receipt", receipt={"sent": True}, handle=idempotency_key)

    def cancel(self, handle: str) -> None:
        self.cancelled.append(handle)
        self._abort.set()

    def execute(self, action: ResolvedAction, scope: ScopePredicate | None, actor: Actor) -> ConnectorResult:
        return ConnectorResult(kind="receipt", receipt={})

    def fetch_target(self, action: ResolvedAction, scope: ScopePredicate | None, actor: Actor) -> dict[str, Any] | None:
        return dict(action.data)


def test_e4_kill_aborts_in_flight_call() -> None:
    reg = full_registry()
    audit = InMemoryAuditSink()
    store = InMemoryOutboxStore(audit=audit)
    conn = _BlockingConnector()
    inflight = InFlightRegistry()
    worker = DispatchWorker(store, Connectors({"email": conn}), registry=reg, inflight=inflight)
    kill = InMemoryKillStore()
    service = KillService(kill, audit=audit, inflight=inflight)

    staged = store.stage(
        resolved=reg.resolve(RawCall(resource="Email", action="sendEmail", data={"to": "x@acme.example"})),
        actor=Actor(id="alice"), session_id="s1", agent="support",
        state=PendingState.PENDING,
    )

    # run the worker in a thread; it will block inside dispatch
    t = threading.Thread(target=worker.run_once)
    t.start()
    assert conn.started.wait(timeout=3.0)  # dispatch is in flight and registered

    # operator issues a SESSION kill ⇒ the in-flight handle is cancelled
    service.issue(KillScope.for_session("s1"), issued_by="operator")
    t.join(timeout=3.0)
    assert not t.is_alive()

    assert conn.cancelled == [staged.idempotency_key]  # the handle was invoked
    row = store.get(staged.id)
    assert row is not None and row.state is PendingState.CANCELLED
    assert any(r.outcome in ("failure", "cancelled") for r in audit.records)


def test_inflight_registry_unregisters_after_successful_dispatch() -> None:
    reg = full_registry()
    store = InMemoryOutboxStore()
    inflight = InFlightRegistry()

    class _FastConnector(_BlockingConnector):
        def dispatch(self, action: ResolvedAction, actor: Actor, idempotency_key: str) -> ConnectorResult:
            return ConnectorResult(kind="receipt", receipt={"sent": True}, handle=idempotency_key)

    worker = DispatchWorker(store, Connectors({"email": _FastConnector()}), registry=reg, inflight=inflight)
    store.stage(
        resolved=reg.resolve(RawCall(resource="Email", action="sendEmail", data={"to": "x"})),
        actor=Actor(id="alice"), session_id="s1", agent="support", state=PendingState.PENDING,
    )
    worker.run_once()
    # nothing left registered after the call returns
    assert inflight.cancel_matching(lambda c: True) == []
