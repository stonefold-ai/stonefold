"""M5 — the operator-facing ``KillService`` (design §8, RFC §9): issuing/lifting a
kill is itself an audited operator action, and issuing optionally fans out to the
defense-in-depth propagation hooks (§8.7: runtime cancel, credential revoke).
"""

from __future__ import annotations

from typing import Any

from stonefold_core import Decision, InMemoryAuditSink
from stonefold_core.kill import KillScope
from stonefold_store.kill_memory import InMemoryKillStore
from stonefold_gateway.kill_service import KillService


class _RecordingRuntimeCancel:
    def __init__(self) -> None:
        self.cancelled: list[str] = []

    def cancel_session(self, session_id: str) -> None:
        self.cancelled.append(session_id)


class _RecordingCredentialRevoke:
    def __init__(self) -> None:
        self.revoked: list[KillScope] = []

    def revoke(self, scope: KillScope) -> None:
        self.revoked.append(scope)


def test_issue_is_audited_as_an_operator_action() -> None:
    audit = InMemoryAuditSink()
    service = KillService(InMemoryKillStore(), audit=audit)
    order = service.issue(KillScope.for_session("s1"), issued_by="alice@ops")

    records = [r for r in audit.records if r.action == "kill.issue"]
    assert len(records) == 1
    rec = records[0]
    assert rec.decision is Decision.HALT
    assert rec.actor == "alice@ops"
    assert order.id in rec.parameters.get("order_id", "")


def test_lift_is_audited() -> None:
    audit = InMemoryAuditSink()
    store = InMemoryKillStore()
    service = KillService(store, audit=audit)
    order = service.issue(KillScope.for_global(), issued_by="op")
    service.lift(order.id, lifted_by="op2")

    assert any(r.action == "kill.lift" for r in audit.records)
    assert service.active() == ()  # nothing active after the lift


def test_propagation_hooks_fire_on_issue() -> None:
    runtime = _RecordingRuntimeCancel()
    creds = _RecordingCredentialRevoke()
    service = KillService(InMemoryKillStore(), runtime_cancel=runtime, credential_revoke=creds)

    scope = KillScope.for_session("s1")
    service.issue(scope, issued_by="op")
    assert runtime.cancelled == ["s1"]  # §8.7 runtime cancel for a session kill
    assert creds.revoked == [scope]  # §8.7 credential revoke


def test_active_lists_only_unlifted_orders() -> None:
    store = InMemoryKillStore()
    service = KillService(store)
    o1 = service.issue(KillScope.for_session("s1"), issued_by="op")
    service.issue(KillScope.for_session("s2"), issued_by="op")
    service.lift(o1.id, lifted_by="op")

    active = service.active()
    assert len(active) == 1
    assert active[0].scope.session_id == "s2"
