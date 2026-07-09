"""v0.6 hold substrate — CS-026/027/028 (changeset docs/RFC-changeset-v0.5-to-v0.6.md).

Preconditions may resolve HOLD (three-valued checks, RFC §7.6); a held row
carries the release contract of EVERY holding gate and promotes only when all
are satisfied (§12, CS-027 — the approval-bypass regression); held rows expire
actively (CS-028). Driven through the in-memory outbox + dispatch worker; the
Postgres approve path shares ``apply_release`` and is exercised in
``test_m4_pg_integration.py``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from stonefold_core import (
    Actor,
    Decision,
    FreshnessConfig,
    InMemoryAuditSink,
    PendingState,
    RawCall,
    RequestEnv,
    SelfApprovalError,
    Session,
    enforce,
    load_policy,
)
from stonefold_core.enums import Outcome
from stonefold_core.freshness import stale_guard_reason
from stonefold_core.outbox import expired_hold_reason
from stonefold_connectors import InMemoryConnector
from stonefold_gates.base import CheckResult, check_fail, check_hold, check_pass
from stonefold_gates.engine import DefaultGateEngine, make_dispatch_revalidator
from stonefold_store import DispatchWorker, InMemoryOutboxStore
from tests.conftest import full_registry, load_schema

T0 = datetime(2026, 7, 9, 9, 0, tzinfo=timezone.utc)
CFG = FreshnessConfig(
    default_ttl=timedelta(hours=24), irreversible_ttl=timedelta(minutes=30)
)
# The scripted check registers under a name the central registry declares
# hold-capable with reason codes (CS-026 rule 3 / CS-029 — a hold from a check
# not declared holdCapable resolves fail-closed).
CHECK = "matchesOpenPurchaseOrder"
# A declared, bare-name (two-valued) check for the bool-compat tests.
BOOL_CHECK = "payeeCoolingOffElapsed"


class ScriptedCheck:
    """A registered check whose verdict the test flips between calls."""

    def __init__(self, result: CheckResult | bool) -> None:
        self.result: CheckResult | bool = result
        self.calls = 0

    def __call__(self, gctx: Any) -> CheckResult | bool:
        self.calls += 1
        return self.result


class CrashingCheck:
    def __call__(self, gctx: Any) -> bool:
        raise ConnectionError("source system unreachable")


@dataclass
class Harness:
    reg: Any
    policy: Any
    audit: InMemoryAuditSink
    outbox: InMemoryOutboxStore
    engine: DefaultGateEngine
    effect_conn: InMemoryConnector

    def enforce(self, data: dict[str, Any] | None = None, *, actor: str = "alice") -> Any:
        return enforce(
            RawCall(resource="Email", action="sendEmail", data=data or {"to": "x@a.example"}),
            Actor(id=actor),
            Session(id="s1", correlation_id="corr-1"),
            registry=self.reg,
            audit=self.audit,
            policy=self.policy,
            gates=self.engine,
            outbox=self.outbox,
            env=RequestEnv(now=T0),
            freshness=CFG,
        )

    def worker_at(self, now: datetime) -> DispatchWorker:
        from stonefold_core import Connectors

        connectors = Connectors(
            {"email": self.effect_conn, "sql": self.effect_conn, "in_memory": self.effect_conn}
        )
        return DispatchWorker(
            self.outbox,
            connectors,
            registry=self.reg,
            clock=lambda: now,
            revalidate=make_dispatch_revalidator(self.engine, self.policy),
        )

    def get(self, ticket: str) -> Any:
        row = self.outbox.get(ticket)
        assert row is not None
        return row


def harness(
    gates: dict[str, Any],
    check: Any,
    *,
    default_resolver_role: str | None = None,
) -> Harness:
    reg = full_registry()
    doc: dict[str, Any] = {
        "agent": "ap-agent",
        "allow": [{"effect": ["sendEmail"]}],
        "gates": {"sendEmail": gates},
    }
    policy = load_policy(doc, reg, schema=load_schema())
    audit = InMemoryAuditSink()
    outbox = InMemoryOutboxStore(audit=audit)
    engine = DefaultGateEngine(
        reg, preconditions={CHECK: check}, default_resolver_role=default_resolver_role
    )
    return Harness(reg, policy, audit, outbox, engine, InMemoryConnector())


HOLD_GATE = {"precondition": {"checks": [CHECK], "resolvers": "role:ap-clerk"}}


# --- CS-026: three-valued checks ------------------------------------------


def test_check_hold_stages_row_with_resolver_contract() -> None:
    check = ScriptedCheck(check_hold("multiple-candidates", {"candidates": ["PO-1", "PO-2"]}))
    h = harness(HOLD_GATE, check)
    result = h.enforce()
    assert result.decision is Decision.HOLD
    assert result.rule == "gate:precondition"

    row = h.get(result.ticket)
    assert row.state is PendingState.PENDING_APPROVAL
    assert len(row.releases) == 1
    contract = row.releases[0]
    assert contract.gate == "precondition"
    assert contract.cause == f"precondition:{CHECK}"
    assert contract.approvers == ("role:ap-clerk",)
    assert contract.reason_code == "multiple-candidates"
    assert contract.evidence == {"candidates": ["PO-1", "PO-2"]}

    # I7: the held-intent audit record carries cause, code, and evidence.
    held_rec = h.audit.records[-1]
    assert held_rec.decision is Decision.HOLD
    assert held_rec.approval is not None
    (rendered,) = held_rec.approval["releases"]
    assert rendered["cause"] == f"precondition:{CHECK}"
    assert rendered["reasonCode"] == "multiple-candidates"
    assert rendered["evidence"] == {"candidates": ["PO-1", "PO-2"]}
    # the gate trace carries the code too (the hold survives into row.gates)
    hold_trace = [g for g in row.gates if g.gate == "precondition"]
    assert hold_trace and hold_trace[0].code == "multiple-candidates"


def test_resolver_release_promotes_and_dispatches() -> None:
    check = ScriptedCheck(check_hold("no-open-match"))
    h = harness(HOLD_GATE, check)
    result = h.enforce()

    check.result = check_pass()  # the world changed: an order now matches
    h.outbox.approve(result.ticket, "clerk-1")
    assert h.get(result.ticket).state is PendingState.PENDING

    h.worker_at(T0 + timedelta(minutes=1)).drain()
    settled = h.get(result.ticket)
    assert settled.state is PendingState.DONE
    assert len(h.effect_conn.effects) == 1
    # I7: the settle audit names the resolver and the contract it satisfied.
    done_rec = h.audit.records[-1]
    assert done_rec.approval is not None
    (rendered,) = done_rec.approval["releases"]
    assert rendered["satisfiedBy"] == ["clerk-1"]
    assert rendered["satisfied"] is True


def test_released_row_that_holds_again_at_dispatch_cancels_stale_guard() -> None:
    # R2/U3: a claimed row is never re-suspended — a fresh hold at the claim is
    # stale, exactly like any other volatile-gate movement (CS-017).
    check = ScriptedCheck(check_hold("multiple-candidates"))
    h = harness(HOLD_GATE, check)
    result = h.enforce()
    h.outbox.approve(result.ticket, "clerk-1")  # released while STILL ambiguous

    h.worker_at(T0 + timedelta(minutes=1)).drain()
    settled = h.get(result.ticket)
    assert settled.state is PendingState.CANCELLED
    assert settled.reason == stale_guard_reason("precondition")
    assert h.effect_conn.effects == []


def test_code_less_hold_is_an_implementation_error(caplog: pytest.LogCaptureFixture) -> None:
    # I4 / CS-026 rule 2: a hold without a machine-readable reason code resolves
    # fail-closed and is logged loudly.
    check = ScriptedCheck(CheckResult(outcome=Outcome.HOLD))
    h = harness(HOLD_GATE, check)
    with caplog.at_level(logging.ERROR, logger="stonefold.gates"):
        result = h.enforce()
    assert result.decision is Decision.DENY
    assert result.rule == "gate:precondition"
    assert any("without a reason code" in r.message for r in caplog.records)


def test_crash_fails_closed_never_holds() -> None:
    # I5 / CS-026 rule 1: a crash is a dependency failure — fail, never hold.
    h = harness(HOLD_GATE, CrashingCheck())
    result = h.enforce()
    assert result.decision is Decision.DENY
    assert result.rule == "gate:precondition"
    assert h.outbox.list_by_state(PendingState.PENDING_APPROVAL) == []


def test_crash_with_failure_mode_open_passes() -> None:
    # §10: failureMode open skips the errored check (low-stakes deployments).
    # Gate-level: a full open-mode policy on an irreversible effect is a §13.5
    # linter error, so the pipeline can't legally reach this combination.
    from stonefold_core.policy import FailureMode
    from stonefold_gates import gates as g
    from tests.conftest import gate_ctx

    gctx = gate_ctx(
        "Email", "sendEmail",
        preconditions={CHECK: CrashingCheck()},
        failure_mode=FailureMode.OPEN,
    )
    result = g.precondition({"checks": [CHECK]}, gctx)
    assert result.outcome is Outcome.PASS


def test_bool_checks_stay_valid() -> None:
    # I6 at unit level: the two-valued, bare-name-declared form is unchanged.
    reg = full_registry()
    doc: dict[str, Any] = {
        "agent": "ap-agent",
        "allow": [{"effect": ["sendEmail"]}],
        "gates": {"sendEmail": {"precondition": [BOOL_CHECK]}},
    }
    policy = load_policy(doc, reg, schema=load_schema())
    for verdict, expected in ((True, Decision.ALLOW), (False, Decision.DENY)):
        audit = InMemoryAuditSink()
        h = Harness(
            reg, policy, audit, InMemoryOutboxStore(audit=audit),
            DefaultGateEngine(reg, preconditions={BOOL_CHECK: ScriptedCheck(verdict)}),
            InMemoryConnector(),
        )
        assert h.enforce().decision is expected


# --- CS-027: multi-hold release contracts ----------------------------------


def test_hold_unresolvable_without_resolvers_or_default() -> None:
    check = ScriptedCheck(check_hold("no-open-match"))
    h = harness({"precondition": {"checks": [CHECK]}}, check)
    result = h.enforce()
    assert result.decision is Decision.DENY
    assert result.rule == "hold-unresolvable"
    assert h.outbox.list_by_state(PendingState.PENDING_APPROVAL) == []
    assert h.audit.records[-1].rule == "hold-unresolvable"


def test_deployment_default_resolver_role_makes_hold_resolvable() -> None:
    check = ScriptedCheck(check_hold("no-open-match"))
    h = harness(
        {"precondition": {"checks": [CHECK]}}, check, default_resolver_role="role:ops"
    )
    result = h.enforce()
    assert result.decision is Decision.HOLD
    assert h.get(result.ticket).releases[0].approvers == ("role:ops",)


def test_precondition_hold_cannot_bypass_dual_authorization() -> None:
    # THE R1 regression: v0.5-style first-hold-wins let a precondition hold carry
    # an empty contract that one self-release would promote past a co-holding
    # approval gate. Now every holding gate binds.
    check = ScriptedCheck(check_hold("multiple-candidates"))
    gates = dict(HOLD_GATE)
    gates["dualAuthorization"] = {"approvers": "role:treasury"}
    h = harness(gates, check)
    result = h.enforce(actor="alice")
    assert result.decision is Decision.HOLD
    row = h.get(result.ticket)
    assert {c.gate for c in row.releases} == {"precondition", "dualAuthorization"}

    check.result = check_pass()
    # the acting principal can resolve the ambiguity contract — via the
    # TARGETED form, the only call shape that credits a check-driven contract
    # (a bare approve targets the approval-shaped contracts only; anything
    # else re-opens the very bypass this test exists for) — but that alone
    # must NOT promote the row...
    h.outbox.approve(result.ticket, "alice", gate="precondition")
    assert h.get(result.ticket).state is PendingState.PENDING_APPROVAL
    # ...and alice can contribute nothing further: only the refusing dual-auth
    # contract remains, and it rejects the actor.
    with pytest.raises(SelfApprovalError):
        h.outbox.approve(result.ticket, "alice")
    # two distinct non-actor identities satisfy the dual contract; all satisfied.
    h.outbox.approve(result.ticket, "treasury-1")
    assert h.get(result.ticket).state is PendingState.PENDING_APPROVAL
    h.outbox.approve(result.ticket, "treasury-2")
    assert h.get(result.ticket).state is PendingState.PENDING


def test_gate_targeted_release_satisfies_that_contract_only() -> None:
    check = ScriptedCheck(check_hold("no-open-match"))
    gates = dict(HOLD_GATE)
    gates["requireApproval"] = {"approvers": "role:manager"}
    h = harness(gates, check)
    result = h.enforce()

    check.result = check_pass()
    h.outbox.approve(result.ticket, "clerk-1", gate="precondition")
    row = h.get(result.ticket)
    assert row.state is PendingState.PENDING_APPROVAL  # approval still owed
    by_gate = {c.gate: c for c in row.releases}
    assert by_gate["precondition"].satisfied
    assert not by_gate["requireApproval"].satisfied

    h.outbox.approve(result.ticket, "boss", gate="requireApproval")
    assert h.get(result.ticket).state is PendingState.PENDING


# --- CS-028: held-row expiry ------------------------------------------------


def test_staging_ttl_expires_held_row_preserving_reason_code() -> None:
    # I2': the sweep cancels a lapsed hold as expired-hold:<gate>, audited, with
    # the original hold reason code preserved in the gates trace.
    check = ScriptedCheck(check_hold("multiple-candidates"))
    h = harness(HOLD_GATE, check)
    result = h.enforce()
    assert h.get(result.ticket).expires_at == T0 + timedelta(minutes=30)  # irreversible TTL

    h.worker_at(T0 + timedelta(minutes=31)).run_once()
    settled = h.get(result.ticket)
    assert settled.state is PendingState.CANCELLED
    assert settled.reason == expired_hold_reason("precondition")
    last = h.audit.records[-1]
    assert last.outcome == "cancelled"
    assert last.rule == expired_hold_reason("precondition")
    hold_trace = [g for g in last.gates if g.gate == "precondition"]
    assert hold_trace and hold_trace[0].code == "multiple-candidates"
    # a late release cannot resurrect the cancelled row (CS-017 rule unchanged)
    with pytest.raises(Exception):
        h.outbox.approve(result.ticket, "clerk-1")


def test_gate_timeout_on_timeout_deny_cancels_before_staging_ttl() -> None:
    check = ScriptedCheck(check_pass())
    gates = {"requireApproval": {"approvers": "role:manager", "timeout": "10m", "onTimeout": "deny"}}
    h = harness(gates, check)
    result = h.enforce()
    assert result.decision is Decision.HOLD

    h.worker_at(T0 + timedelta(minutes=11)).run_once()
    settled = h.get(result.ticket)
    assert settled.state is PendingState.CANCELLED
    assert settled.reason == expired_hold_reason("requireApproval")


def test_on_timeout_allow_satisfies_its_own_contract_only() -> None:
    # CS-028: onTimeout: allow promotes iff every OTHER contract is satisfied.
    check = ScriptedCheck(check_hold("no-open-match"))
    gates = dict(HOLD_GATE)
    gates["requireApproval"] = {
        "approvers": "role:manager", "timeout": "10m", "onTimeout": "allow",
    }
    h = harness(gates, check)
    result = h.enforce()

    check.result = check_pass()
    h.worker_at(T0 + timedelta(minutes=11)).sweep_expired_holds()
    row = h.get(result.ticket)
    assert row.state is PendingState.PENDING_APPROVAL  # precondition still owed
    by_gate = {c.gate: c for c in row.releases}
    assert by_gate["requireApproval"].satisfied_by == ("system:timeout",)
    assert not by_gate["precondition"].satisfied

    h.outbox.approve(result.ticket, "clerk-1", gate="precondition")
    assert h.get(result.ticket).state is PendingState.PENDING


def test_kill_covers_precondition_holds_exactly_like_approval_holds() -> None:
    # I3: a released precondition hold meets the kill switch at the claim, the
    # same path a released approval does.
    check = ScriptedCheck(check_hold("no-open-match"))
    h = harness(HOLD_GATE, check)
    result = h.enforce()
    check.result = check_pass()
    h.outbox.approve(result.ticket, "clerk-1")

    h.worker_at(T0 + timedelta(minutes=1)).run_once(kill_check=lambda row: True)
    settled = h.get(result.ticket)
    assert settled.state is PendingState.CANCELLED
    assert settled.reason == "kill"
    assert h.effect_conn.effects == []


# --- the emissionControl checks-key fix (found during CS-026) ---------------


def test_emission_control_runs_declared_checks() -> None:
    # Pre-v0.6 the gate read a "precondition:" key that was never legal syntax,
    # silently skipping every declared check. RFC §7.13 spells it "checks:".
    failing = ScriptedCheck(check_fail("emcon-denied"))
    reg = full_registry()
    doc = {
        "agent": "ops",
        "allow": [{"effect": ["sendEmail"]}],
        "gates": {"sendEmail": {"emissionControl": {"checks": ["emconAuthorized"]}}},
    }
    policy = load_policy(doc, reg, schema=load_schema())
    audit = InMemoryAuditSink()
    engine = DefaultGateEngine(reg, preconditions={"emconAuthorized": failing})
    result = enforce(
        RawCall(resource="Email", action="sendEmail", data={}),
        Actor(id="alice"),
        Session(id="s1"),
        registry=reg,
        audit=audit,
        policy=policy,
        gates=engine,
        env=RequestEnv(now=T0),
    )
    assert result.decision is Decision.DENY
    assert result.rule == "gate:emissionControl"
    assert failing.calls == 1
