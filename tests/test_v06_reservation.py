"""v0.6 reservation lifecycle — CS-035 (+CS-037 consumption audit; R6 orphan
recovery; F5.2 clock skew; CS-023 batch composition).

Obligation state tracks the staged effect's lifecycle exactly: reserve before
the staging commit returns, liveness at the dispatch claim, consume with the
successful settle, release on every terminal non-success — driven here through
fault injection: crashes between reserve and commit, double-submits, TTL
expiry, connector failure, adapter/gateway clock skew.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from stonefold_core import (
    Actor,
    Decision,
    FreshnessConfig,
    InMemoryAuditSink,
    PendingState,
    RawCall,
    RequestEnv,
    RetryClass,
    Session,
    enforce,
    enforce_batch,
    load_policy,
)
from stonefold_connectors import InMemoryConnector
from stonefold_gates.engine import DefaultGateEngine, make_dispatch_revalidator
from stonefold_store import (
    DispatchWorker,
    InMemoryObligationRegistry,
    InMemoryOutboxStore,
)
from tests.conftest import full_registry, load_schema
from tests.test_v06_require_match import (
    GOOD_DATA,
    MATCH_CFG,
    PO_FIELDS,
    PO_REGISTRY,
)

T0 = datetime(2026, 7, 9, 9, 0, tzinfo=timezone.utc)
FRESHNESS = FreshnessConfig(
    default_ttl=timedelta(hours=24), irreversible_ttl=timedelta(minutes=30)
)
RESERVATION_TTL_S = 26 * 3600.0  # >= the row's decision TTL (CS-035 R6)

POLICY_DATA: dict[str, Any] = {
    "apiVersion": "stele/v0.1",
    "agent": "reservation-test-agent",
    "defaults": {"failureMode": "closed", "audit": "full"},
    "allow": [{"effect": ["pay"]}],
    "gates": {"pay": {"requireMatch": dict(MATCH_CFG)}},
}


class FailingConnector:
    """A connector whose dispatch always fails — the effect never lands."""

    def dispatch(self, resolved: Any, actor: Any, idempotency_key: str) -> Any:
        raise RuntimeError("wire transfer service down")


@dataclass
class Harness:
    reg: Any
    policy: Any
    audit: InMemoryAuditSink
    outbox: InMemoryOutboxStore
    engine: DefaultGateEngine
    adapter: InMemoryObligationRegistry
    adapter_now: list[datetime] = field(default_factory=lambda: [T0])

    def enforce(self, data: dict[str, Any] | None = None, *, session: str = "s1") -> Any:
        return enforce(
            RawCall(resource="Payment", action="pay", data=data or dict(GOOD_DATA)),
            Actor(id="alice"),
            Session(id=session, correlation_id=session),
            registry=self.reg,
            audit=self.audit,
            policy=self.policy,
            gates=self.engine,
            outbox=self.outbox,
            env=RequestEnv(now=T0),
            freshness=FRESHNESS,
            obligations={PO_REGISTRY: self.adapter},
        )

    def worker_at(self, now: datetime, *, connector: Any | None = None) -> DispatchWorker:
        from stonefold_core import Connectors

        conn = connector if connector is not None else InMemoryConnector()
        connectors = Connectors({"email": conn, "sql": conn, "in_memory": conn})
        return DispatchWorker(
            self.outbox,
            connectors,
            registry=self.reg,
            clock=lambda: now,
            revalidate=make_dispatch_revalidator(self.engine, self.policy),
            obligations={PO_REGISTRY: self.adapter},
        )

    def line_state(self, ref: str = "po-1") -> Any:
        from stonefold_core.obligation import lookup_field

        return lookup_field(self.adapter._records[ref], "line.state")

    def row(self, ticket: str) -> Any:
        row = self.outbox.get(ticket)
        assert row is not None
        return row


def harness(
    policy_data: dict[str, Any] | None = None,
    records: dict[str, dict[str, Any]] | None = None,
    *,
    reservation_ttl_s: float | None = RESERVATION_TTL_S,
) -> Harness:
    reg = full_registry()
    policy = load_policy(policy_data or POLICY_DATA, reg, schema=load_schema())
    adapter_now = [T0]
    adapter = InMemoryObligationRegistry(
        records if records is not None else {"po-1": {**PO_FIELDS, "line": dict(PO_FIELDS["line"])}},
        state_path="line.state",
        reservation_ttl_s=reservation_ttl_s,
        clock=lambda: adapter_now[0],
    )
    engine = DefaultGateEngine(reg, obligations={PO_REGISTRY: adapter})
    audit = InMemoryAuditSink()
    h = Harness(reg, policy, audit, InMemoryOutboxStore(audit), engine, adapter)
    h.adapter_now = adapter_now
    return h


# ==========================================================================
# Reserve at staging (CS-035): before the commit returns, visible to queries
# ==========================================================================
class TestReserveAtStaging:
    def test_staged_row_carries_the_claim_and_the_line_reserves(self) -> None:
        h = harness()
        result = h.enforce()
        assert result.decision is Decision.ALLOW and result.ticket is not None
        row = h.row(result.ticket)
        assert row.obligation is not None
        assert row.obligation.registry == PO_REGISTRY
        assert row.obligation.ref == "po-1"
        assert row.obligation.consume == "obligation.line"
        assert h.line_state() == "reserved"
        rec = h.audit.records[-1]
        assert rec.consumption is not None
        assert rec.consumption["state"] == "reserved"
        assert rec.consumption["ref"] == "po-1"

    def test_double_submit_no_matches_at_decision_time(self) -> None:
        # the reserved line's state moved, so the second intent finds no open
        # obligation at DECISION time — one line, one payment (the double-spend
        # window the decision TTL alone does not close).
        h = harness()
        assert h.enforce().decision is Decision.ALLOW
        second = h.enforce(session="s2")
        assert second.decision is Decision.DENY
        assert second.reason_code == "no-match"
        assert second.retry_class is RetryClass.TERMINAL

    def test_commit_race_settles_refused_no_match(self) -> None:
        # decision saw the line free; another intent reserved it between
        # decision and staging (injected: no state_path visibility) ⇒ the
        # staging reservation is refused and the intent settles no-match.
        h = harness()
        # hide reservations from the query so the race window exists
        h.adapter._state_path = None
        assert h.enforce().decision is Decision.ALLOW
        second = h.enforce(session="s2")
        assert second.decision is Decision.DENY
        assert second.rule == "no-match"
        assert second.ticket is None  # never staged
        assert h.audit.records[-1].outcome == "not_executed"

    def test_stage_failure_after_reserve_releases_the_line(self) -> None:
        h = harness()

        def broken_stage(**_kw: Any) -> Any:
            raise ConnectionError("outbox db down")

        h.outbox.stage = broken_stage  # type: ignore[method-assign]
        result = h.enforce()
        assert result.decision is Decision.DENY
        assert result.rule == "outbox-unavailable"
        assert h.line_state() == "unconsumed"  # released, not stranded

    def test_missing_adapter_refuses_fail_closed_even_open(self) -> None:
        # reservation has no failureMode escape: staging a consumable match
        # without its reservation would reopen the double-spend window.
        data = dict(POLICY_DATA)
        data["defaults"] = {"failureMode": "open", "audit": "full"}
        h = harness(data)
        h.engine.obligations = {PO_REGISTRY: h.adapter}  # gate passes...
        result = enforce(
            RawCall(resource="Payment", action="pay", data=dict(GOOD_DATA)),
            Actor(id="alice"),
            Session(id="s1"),
            registry=h.reg, audit=h.audit, policy=h.policy, gates=h.engine,
            outbox=h.outbox, env=RequestEnv(now=T0), freshness=FRESHNESS,
            obligations=None,  # ...but the commit phase has no adapter to reserve from
        )
        assert result.decision is Decision.DENY
        assert result.rule == "reservation-unavailable"


# ==========================================================================
# Consume at settle (CS-035/CS-037): with the effect, never without it
# ==========================================================================
class TestConsumeAtSettle:
    def test_successful_dispatch_consumes_with_receipt(self) -> None:
        h = harness()
        result = h.enforce()
        assert h.worker_at(T0 + timedelta(minutes=1)).run_once()
        assert h.row(result.ticket).state is PendingState.DONE
        assert h.line_state() == "consumed"
        settle = h.audit.records[-1]
        assert settle.outcome == "success"
        assert settle.consumption is not None
        assert settle.consumption["state"] == "consumed"
        assert settle.consumption["receipt"]
        assert settle.consumption["capability"] == "transactional"

    def test_consumed_line_refuses_resubmission_no_match(self) -> None:
        # the §14.4 resubmit beat: pay → consumed; the same invoice again ⇒
        # no obligation left to match.
        h = harness()
        h.enforce()
        h.worker_at(T0 + timedelta(minutes=1)).run_once()
        again = h.enforce(session="s2")
        assert again.decision is Decision.DENY
        assert again.reason_code == "no-match"

    def test_connector_failure_releases_and_never_consumes(self) -> None:
        # no consumed-without-effect: a failed dispatch settles FAILED, the
        # reservation is returned, and the line frees for a retry.
        h = harness()
        result = h.enforce()
        assert h.worker_at(T0 + timedelta(minutes=1), connector=FailingConnector()).run_once()
        row = h.row(result.ticket)
        assert row.state is PendingState.FAILED
        assert h.line_state() == "unconsumed"
        settle = h.audit.records[-1]
        assert settle.consumption is not None and settle.consumption["state"] == "released"
        # the freed line accepts a fresh intent
        assert h.enforce(session="s2").decision is Decision.ALLOW

    def test_held_row_reserves_and_consumes_after_approval(self) -> None:
        data = dict(POLICY_DATA)
        data["gates"] = {
            "pay": {
                "requireMatch": dict(MATCH_CFG),
                "requireApproval": {"approvers": "role:payments-manager"},
            }
        }
        h = harness(data)
        result = h.enforce()
        assert result.decision is Decision.HOLD
        assert h.line_state() == "reserved"  # held rows hold their line too
        h.outbox.approve(result.ticket, "manager-1")
        assert h.worker_at(T0 + timedelta(minutes=5)).run_once()
        assert h.row(result.ticket).state is PendingState.DONE
        assert h.line_state() == "consumed"


# ==========================================================================
# Release on terminal non-success (CS-035): kill, stale, expiry, rejection
# ==========================================================================
class TestRelease:
    def test_rejection_releases_the_line_for_resubmission(self) -> None:
        data = dict(POLICY_DATA)
        data["gates"] = {
            "pay": {
                "requireMatch": dict(MATCH_CFG),
                "requireApproval": {"approvers": "role:payments-manager"},
            }
        }
        h = harness(data)
        result = h.enforce()
        assert result.decision is Decision.HOLD
        h.outbox.reject(result.ticket, "manager-1")
        assert h.line_state() == "reserved"  # not yet: reject is transport-side
        h.worker_at(T0 + timedelta(minutes=1)).run_once()  # the reconcile sweep
        assert h.line_state() == "unconsumed"
        # the freed line matches a fresh intent again — which this policy then
        # holds at requireApproval (proving requireMatch passed and re-reserved).
        retry = h.enforce(session="s2")
        assert retry.decision is Decision.HOLD
        assert retry.rule == "gate:requireApproval"
        assert h.line_state() == "reserved"

    def test_staging_ttl_expiry_frees_the_line(self) -> None:
        h = harness()
        result = h.enforce()
        # the decision TTL (24h) lapses; the claim cancels stale-decision and
        # the same run's release pass frees the line.
        h.adapter_now[0] = T0 + timedelta(hours=25)
        h.worker_at(T0 + timedelta(hours=25)).run_once()
        row = h.row(result.ticket)
        assert row.state is PendingState.CANCELLED
        assert row.reason == "stale-decision"
        assert h.line_state() == "unconsumed"

    def test_expired_hold_releases(self) -> None:
        data = dict(POLICY_DATA)
        data["gates"] = {
            "pay": {
                "requireMatch": dict(MATCH_CFG),
                "requireApproval": {
                    "approvers": "role:payments-manager",
                    "timeout": "10m", "onTimeout": "deny",
                },
            }
        }
        h = harness(data)
        result = h.enforce()
        assert result.decision is Decision.HOLD
        h.worker_at(T0 + timedelta(minutes=11)).run_once()
        row = h.row(result.ticket)
        assert row.state is PendingState.CANCELLED
        assert row.reason == "expired-hold:requireApproval"
        assert h.line_state() == "unconsumed"

    def test_kill_at_claim_releases(self) -> None:
        h = harness()
        result = h.enforce()
        h.worker_at(T0 + timedelta(minutes=1)).run_once(kill_check=lambda row: True)
        row = h.row(result.ticket)
        assert row.state is PendingState.CANCELLED
        assert h.line_state() == "unconsumed"

    def test_release_is_idempotent_across_restart(self) -> None:
        # a new worker (empty release cache) re-releases every terminal claim:
        # all NotHeld no-ops — the CS-035 restart reconciliation.
        h = harness()
        result = h.enforce()
        h.worker_at(T0 + timedelta(minutes=1), connector=FailingConnector()).run_once()
        assert h.line_state() == "unconsumed"
        restarted = h.worker_at(T0 + timedelta(minutes=2))
        assert restarted.release_terminal_claims() == 1  # re-released, no-op
        assert h.line_state() == "unconsumed"
        assert h.row(result.ticket).state is PendingState.FAILED


# ==========================================================================
# R6 + F5.2: orphan recovery and adapter/gateway clock skew
# ==========================================================================
class TestOrphansAndSkew:
    def test_crash_orphan_expires_by_adapter_ttl(self) -> None:
        # a crash between reserve and the staging commit leaves a reservation
        # with no row; the adapter's own TTL frees the real line.
        h = harness(reservation_ttl_s=3600.0)
        assert h.adapter.reserve("po-1", "crashed-intent").value == "reserved"
        assert h.line_state() == "reserved"
        # a second intent is refused while the orphan is live...
        blocked = h.enforce()
        assert blocked.decision is Decision.DENY and blocked.reason_code == "no-match"
        # ...and admitted once the adapter's clock passes the TTL.
        h.adapter_now[0] = T0 + timedelta(hours=2)
        assert h.enforce(session="s2").decision is Decision.ALLOW

    def test_adapter_expired_but_unclaimed_reservation_reacquires_at_dispatch(self) -> None:
        # F5.2 skew: the reservation expired adapter-side while the row is
        # still live gateway-side. Nobody else took the line, so the liveness
        # probe legitimately re-reserves and the dispatch proceeds.
        h = harness(reservation_ttl_s=600.0)  # 10 min, shorter than the row TTL
        result = h.enforce()
        h.adapter_now[0] = T0 + timedelta(minutes=30)  # reservation expired
        assert h.worker_at(T0 + timedelta(minutes=30)).run_once()
        assert h.row(result.ticket).state is PendingState.DONE
        assert h.line_state() == "consumed"

    def test_adapter_expired_and_stolen_cancels_stale_guard(self) -> None:
        # F5.2 skew, the losing side: the expired line went to another intent
        # before dispatch ⇒ the claim cancels stale-guard:requireMatch.
        h = harness(reservation_ttl_s=600.0)
        result = h.enforce()
        h.adapter_now[0] = T0 + timedelta(minutes=30)
        second = h.enforce(session="s2")  # takes the expired line
        assert second.decision is Decision.ALLOW
        h.worker_at(T0 + timedelta(minutes=31)).run_once()
        rows = {r.id: r for r in (h.row(result.ticket), h.row(second.ticket))}
        first_row = rows[result.ticket]
        assert first_row.state is PendingState.CANCELLED
        assert first_row.reason == "stale-guard:requireMatch"

    def test_release_of_adapter_expired_reservation_is_not_held(self) -> None:
        from stonefold_core.obligation import ReleaseOutcome

        h = harness(reservation_ttl_s=600.0)
        h.adapter.reserve("po-1", "i-1")
        h.adapter_now[0] = T0 + timedelta(hours=1)
        assert h.adapter.release("po-1", "i-1") is ReleaseOutcome.NOT_HELD


# ==========================================================================
# CS-023 composition: batch reservations are all-or-nothing
# ==========================================================================
class TestBatch:
    def test_refused_reservation_refuses_the_batch_and_releases_all(self) -> None:
        # two operations matching the SAME open line: both decide ALLOW (the
        # line is free at decision time), the pre-pass reserves for the first,
        # the second's reservation is refused ⇒ the whole batch refuses and
        # the first reservation is returned — no partial claim survives.
        h = harness()
        calls = [
            RawCall(resource="Payment", action="pay", data=dict(GOOD_DATA)),
            RawCall(resource="Payment", action="pay", data=dict(GOOD_DATA)),
        ]
        batch = enforce_batch(
            calls,
            Actor(id="alice"),
            Session(id="b1", correlation_id="b1"),
            registry=h.reg, audit=h.audit, policy=h.policy, gates=h.engine,
            outbox=h.outbox, envs=[RequestEnv(now=T0)] * 2, freshness=FRESHNESS,
            obligations={PO_REGISTRY: h.adapter},
        )
        assert batch.decision is Decision.DENY
        assert batch.failing_index == 1
        assert batch.results[1].rule == "no-match"
        assert h.line_state() == "unconsumed"  # the first reservation released
        assert h.outbox.list_by_state(PendingState.PENDING) == []  # nothing staged
        refused = [r for r in h.audit.records if r.outcome == "batch-refused"]
        assert len(refused) == 1

    def test_clean_batch_stages_with_claims(self) -> None:
        h = harness(
            records={
                "po-1": {**PO_FIELDS, "line": dict(PO_FIELDS["line"])},
                "po-2": {
                    **PO_FIELDS, "vendorId": "V-88",
                    "line": {"amount": 500.0, "state": "unconsumed"},
                },
            }
        )
        calls = [
            RawCall(resource="Payment", action="pay", data=dict(GOOD_DATA)),
            RawCall(
                resource="Payment", action="pay",
                data={**GOOD_DATA, "vendorId": "V-88", "amount": 500},
            ),
        ]
        batch = enforce_batch(
            calls,
            Actor(id="alice"),
            Session(id="b2", correlation_id="b2"),
            registry=h.reg, audit=h.audit, policy=h.policy, gates=h.engine,
            outbox=h.outbox, envs=[RequestEnv(now=T0)] * 2, freshness=FRESHNESS,
            obligations={PO_REGISTRY: h.adapter},
        )
        assert batch.decision is Decision.ALLOW
        staged = h.outbox.list_by_state(PendingState.PENDING)
        assert len(staged) == 2
        assert {r.obligation.ref for r in staged if r.obligation} == {"po-1", "po-2"}
        assert h.line_state("po-1") == "reserved"
        assert h.line_state("po-2") == "reserved"
