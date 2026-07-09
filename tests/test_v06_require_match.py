"""v0.6 obligation matching — CS-032/033/034/036/037/038 (changeset
docs/RFC-changeset-v0.5-to-v0.6.md; RFC §7.16, §13 rules 14–17, §11
``obligationRefs``).

Decision-time matching only (Phase 5): typed selector → candidate count →
onNoMatch/onAmbiguous/full-conjunction evaluation against the re-read record;
``consume: none`` verification end-to-end; the reservation lifecycle (CS-035)
is exercised when the staging/dispatch/settle wiring lands.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from stonefold_core import (
    Actor,
    Decision,
    EqConstraint,
    FreshnessConfig,
    InMemoryAuditSink,
    PendingState,
    RawCall,
    RegistryFile,
    RequestEnv,
    RetryClass,
    Session,
    enforce,
    load_policy,
    load_registry,
)
from stonefold_core.enums import Outcome
from stonefold_core.freshness import VOLATILE_GATES
from stonefold_core.linter import Severity, lint
from stonefold_core.loader import validate_only
from stonefold_core.obligation import (
    ConsumeOutcome,
    ReleaseOutcome,
    ReserveOutcome,
)
from stonefold_core.policy import Policy
from stonefold_connectors import InMemoryConnector
from stonefold_gates import gates as g
from stonefold_gates.engine import DefaultGateEngine, make_dispatch_revalidator
from stonefold_store import (
    DispatchWorker,
    InMemoryObligationRegistry,
    InMemoryOutboxStore,
)
from tests.conftest import full_registry, gate_ctx, load_schema

T0 = datetime(2026, 7, 9, 9, 0, tzinfo=timezone.utc)
CFG_FRESHNESS = FreshnessConfig(
    default_ttl=timedelta(hours=24), irreversible_ttl=timedelta(minutes=30)
)

PO_REGISTRY = "erp.purchase_orders"

# One open purchase order with one unconsumed $800 line (the RFC §14.4 beat).
PO_FIELDS: dict[str, Any] = {
    "vendorId": "V-77",
    "state": "open",
    "vendor": {"domain": "acme.example"},
    "line": {"amount": 800.0, "state": "unconsumed"},
}

MATCH_CFG: dict[str, Any] = {
    "registry": PO_REGISTRY,
    "match": [
        "obligation.vendorId == data.vendorId",
        "obligation.state == 'open'",
        "obligation.line.state == 'unconsumed'",
        {"field": "obligation.line.amount", "matches": "data.amount", "within": "10%"},
    ],
    "provenance": ["obligation.vendor.domain == data.sourceDomain"],
    "consume": "obligation.line",
    "onNoMatch": "deny",
    "resolvers": "role:ap-clerk",
}

GOOD_DATA: dict[str, Any] = {
    "vendorId": "V-77",
    "amount": 800,
    "sourceDomain": "acme.example",
}


def po_adapter(**records: dict[str, Any]) -> InMemoryObligationRegistry:
    return InMemoryObligationRegistry(records or {"po-1": PO_FIELDS})


def run_gate(
    cfg: dict[str, Any],
    data: dict[str, Any],
    adapter: InMemoryObligationRegistry | None,
    *,
    resource: str = "Payment",
    action: str = "pay",
    failure_mode: Any = None,
) -> Any:
    from stonefold_core.policy import FailureMode

    gctx = gate_ctx(
        resource,
        action,
        data=data,
        obligations={PO_REGISTRY: adapter} if adapter is not None else {},
        failure_mode=failure_mode or FailureMode.CLOSED,
    )
    return g.require_match(cfg, gctx)


# ==========================================================================
# CS-034 — the in-memory adapter (query + idempotent reserve/consume/release)
# ==========================================================================
class TestInMemoryAdapter:
    def test_query_filters_by_equality_constraints(self) -> None:
        reg = po_adapter(
            po1=dict(PO_FIELDS),
            po2={**PO_FIELDS, "vendorId": "V-99"},
        )
        got = reg.query((EqConstraint("vendorId", "V-77"), EqConstraint("state", "open")))
        assert [o.ref for o in got] == ["po1"]

    def test_query_numeric_coercion_matches_condition_language(self) -> None:
        reg = po_adapter(po1=dict(PO_FIELDS))
        got = reg.query((EqConstraint("line.amount", "800"),))
        assert [o.ref for o in got] == ["po1"]

    def test_missing_or_null_field_never_matches(self) -> None:
        reg = po_adapter(po1={"vendorId": None}, po2={})
        assert reg.query((EqConstraint("vendorId", "V-77"),)) == []

    def test_reserve_is_idempotent_per_intent(self) -> None:
        reg = po_adapter()
        assert reg.reserve("po-1", "i-1") is ReserveOutcome.RESERVED
        assert reg.reserve("po-1", "i-1") is ReserveOutcome.RESERVED
        assert reg.reserve("po-1", "i-2") is ReserveOutcome.ALREADY_RESERVED

    def test_consume_retry_returns_same_receipt(self) -> None:
        reg = po_adapter()
        reg.reserve("po-1", "i-1")
        first = reg.consume("po-1", "i-1")
        again = reg.consume("po-1", "i-1")
        assert first.outcome is ConsumeOutcome.CONSUMED
        assert again.outcome is ConsumeOutcome.CONSUMED
        assert first.receipt == again.receipt
        assert reg.consume("po-1", "i-2").outcome is ConsumeOutcome.ALREADY_CONSUMED
        assert reg.reserve("po-1", "i-3") is ReserveOutcome.ALREADY_CONSUMED

    def test_release_is_an_idempotent_no_op_when_not_held(self) -> None:
        reg = po_adapter()
        assert reg.release("po-1", "i-1") is ReleaseOutcome.NOT_HELD
        reg.reserve("po-1", "i-1")
        assert reg.release("po-1", "i-2") is ReleaseOutcome.NOT_HELD
        assert reg.release("po-1", "i-1") is ReleaseOutcome.RELEASED
        # a released line is reservable by the next intent (CS-035 resubmit beat)
        assert reg.reserve("po-1", "i-2") is ReserveOutcome.RESERVED


# ==========================================================================
# CS-034 — declaration parsing (docs/06 §5b)
# ==========================================================================
class TestDeclaration:
    def test_central_registry_declares_the_example_registries(self) -> None:
        reg = full_registry()
        assert reg.has_obligation_registry(PO_REGISTRY)
        assert reg.has_obligation_registry("emr.prescriptions")
        decl = reg.obligation_registry(PO_REGISTRY)
        assert decl is not None
        assert decl.connector == "erp-po-adapter"
        assert decl.has_path("line.amount")
        assert decl.is_numeric("line.amount")
        assert not decl.is_numeric("line.state")  # a values enum is not numeric
        assert not decl.has_path("line.nonexistent")

    def test_digest_pin_merges_into_connector_digests(self) -> None:
        digest = "sha256:" + "a" * 64
        rf = RegistryFile.model_validate(
            {
                "resources": {},
                "connectors": ["adapter"],
                "obligationRegistries": {
                    "erp.pos": {
                        "connector": "adapter",
                        "digest": digest,
                        "capability": "window",
                        "schema": {"vendorId": {"type": "string"}},
                    }
                },
            }
        )
        assert rf.connector_digests["adapter"] == digest

    def test_undeclared_adapter_connector_is_a_load_error(self) -> None:
        with pytest.raises(Exception, match="undeclared connector"):
            RegistryFile.model_validate(
                {
                    "resources": {},
                    "connectors": ["sql"],
                    "obligationRegistries": {
                        "erp.pos": {
                            "connector": "ghost",
                            "capability": "transactional",
                            "schema": {"vendorId": {"type": "string"}},
                        }
                    },
                }
            )


# ==========================================================================
# CS-032/033/036 — the gate (§7.16 semantics, gate-level)
# ==========================================================================
class TestRequireMatchGate:
    def test_unique_match_within_tolerance_passes_with_lineage(self) -> None:
        result = run_gate(MATCH_CFG, GOOD_DATA, po_adapter())
        assert result.outcome is Outcome.PASS
        # a PASS carries lineage + the consumption PLAN (CS-035: the staging
        # commit reserves from consume/capability).
        assert result.evidence == {
            "registry": PO_REGISTRY, "refs": ["po-1"], "candidates": 1,
            "consume": "obligation.line", "capability": "transactional",
        }

    def test_tolerance_is_relative_to_the_obligation_side(self) -> None:
        # 10% of the $800 obligation ⇒ $880 passes, $881 fails.
        ok = run_gate(MATCH_CFG, {**GOOD_DATA, "amount": 880}, po_adapter())
        assert ok.outcome is Outcome.PASS
        bad = run_gate(MATCH_CFG, {**GOOD_DATA, "amount": 881}, po_adapter())
        assert bad.outcome is Outcome.FAIL
        assert bad.code == "outside-tolerance"
        assert bad.retry_class is RetryClass.RETRYABLE  # fixable: edit and resubmit
        assert bad.fields == ("data.amount",)

    def test_within_zero_means_exact(self) -> None:
        cfg = {
            **MATCH_CFG,
            "match": [
                "obligation.vendorId == data.vendorId",
                {"field": "obligation.line.amount", "matches": "data.amount", "within": 0},
            ],
        }
        assert run_gate(cfg, GOOD_DATA, po_adapter()).outcome is Outcome.PASS
        off = run_gate(cfg, {**GOOD_DATA, "amount": 800.01}, po_adapter())
        assert off.outcome is Outcome.FAIL and off.code == "outside-tolerance"

    def test_no_candidates_denies_terminal_by_default(self) -> None:
        result = run_gate(MATCH_CFG, {**GOOD_DATA, "vendorId": "V-00"}, po_adapter())
        assert result.outcome is Outcome.FAIL
        assert result.code == "no-match"
        assert result.retry_class is RetryClass.TERMINAL  # no order exists: don't retry
        assert result.evidence == {"registry": PO_REGISTRY, "refs": [], "candidates": 0}

    def test_on_no_match_hold_suspends_instead(self) -> None:
        cfg = {**MATCH_CFG, "onNoMatch": "hold"}
        result = run_gate(cfg, {**GOOD_DATA, "vendorId": "V-00"}, po_adapter())
        assert result.outcome is Outcome.HOLD
        assert result.code == "no-match"

    def test_ambiguous_holds_by_default_and_never_picks(self) -> None:
        adapter = po_adapter(po1=dict(PO_FIELDS), po2=dict(PO_FIELDS))
        result = run_gate(MATCH_CFG, GOOD_DATA, adapter)
        assert result.outcome is Outcome.HOLD
        assert result.code == "ambiguous-match"
        assert result.evidence is not None and result.evidence["candidates"] == 2
        assert sorted(result.evidence["refs"]) == ["po1", "po2"]

    def test_on_ambiguous_deny_escalates(self) -> None:
        adapter = po_adapter(po1=dict(PO_FIELDS), po2=dict(PO_FIELDS))
        cfg = {**MATCH_CFG, "onAmbiguous": "deny"}
        result = run_gate(cfg, GOOD_DATA, adapter)
        assert result.outcome is Outcome.FAIL
        assert result.code == "ambiguous-match"
        assert result.retry_class is RetryClass.ESCALATE

    def test_on_ambiguous_allow_fails_closed(self) -> None:
        cfg = {**MATCH_CFG, "onAmbiguous": "allow"}
        result = run_gate(cfg, GOOD_DATA, po_adapter())
        assert result.outcome is Outcome.FAIL
        assert "illegal" in result.reason

    def test_provenance_mismatch_is_terminal(self) -> None:
        # A real PO ref, but the invoice's declared source is a different
        # counterparty — the valid-but-wrong-pointer class (§7.16).
        result = run_gate(
            MATCH_CFG, {**GOOD_DATA, "sourceDomain": "evil.example"}, po_adapter()
        )
        assert result.outcome is Outcome.FAIL
        assert result.code == "provenance-mismatch"
        assert result.retry_class is RetryClass.TERMINAL
        assert result.fields == ("data.sourceDomain",)

    def test_forged_obligation_copy_in_data_changes_nothing(self) -> None:
        # CS-036: the agent ships a flattering copy of the obligation in its
        # own payload; every obligation.* operand still resolves from the
        # registry's response, so the verdict is unchanged.
        forged = {
            **GOOD_DATA,
            "amount": 5000,
            "obligation": {"line": {"amount": 5000.0}},  # ignored: just data
        }
        result = run_gate(MATCH_CFG, forged, po_adapter())
        assert result.outcome is Outcome.FAIL
        assert result.code == "outside-tolerance"

    def test_pointer_narrows_but_never_substitutes(self) -> None:
        # An intent-supplied pointer is just another equality clause: it
        # narrows the query, and the remaining conjunction still evaluates
        # against the re-read record (CS-036). Two identical open lines are
        # ambiguous without the pointer; with it, exactly one matches — and a
        # pointer at a record that fails the rest of the match is no-match,
        # never "trust the pointer".
        records = {
            "po1": {**PO_FIELDS, "poId": "po1"},
            "po2": {**PO_FIELDS, "poId": "po2"},
        }
        adapter = po_adapter(**records)
        assert run_gate(MATCH_CFG, GOOD_DATA, adapter).outcome is Outcome.HOLD
        cfg = {
            **MATCH_CFG,
            "match": ["obligation.poId == data.poId", *MATCH_CFG["match"]],
        }
        narrowed = run_gate(cfg, {**GOOD_DATA, "poId": "po2"}, adapter)
        assert narrowed.outcome is Outcome.PASS
        assert narrowed.evidence is not None and narrowed.evidence["refs"] == ["po2"]
        wrong = run_gate(
            cfg, {**GOOD_DATA, "poId": "po2", "vendorId": "V-99"}, adapter
        )
        assert wrong.outcome is Outcome.FAIL and wrong.code == "no-match"

    def test_missing_intent_field_fails_closed(self) -> None:
        data = {k: v for k, v in GOOD_DATA.items() if k != "vendorId"}
        result = run_gate(MATCH_CFG, data, po_adapter())
        assert result.outcome is Outcome.FAIL
        assert "fail-closed" in result.reason

    def test_null_obligation_path_on_matched_record_fails_closed(self) -> None:
        # the matched record exists but the tolerance field is null (CS-032
        # semantics 4: absent or null fails the gate closed).
        broken = {**PO_FIELDS, "line": {"amount": None, "state": "unconsumed"}}
        cfg = {
            **MATCH_CFG,
            "match": [
                "obligation.vendorId == data.vendorId",
                {"field": "obligation.line.amount", "matches": "data.amount", "within": "10%"},
            ],
        }
        result = run_gate(cfg, GOOD_DATA, po_adapter(po1=broken))
        assert result.outcome is Outcome.FAIL
        assert "absent/null" in result.reason

    def test_unknown_registry_fails_closed(self) -> None:
        cfg = {**MATCH_CFG, "registry": "erp.ghost"}
        result = run_gate(cfg, GOOD_DATA, po_adapter())
        assert result.outcome is Outcome.FAIL
        assert "unknown obligation registry" in result.reason

    def test_registry_outage_honours_failure_mode_with_irreversible_floor(self) -> None:
        from stonefold_core.policy import FailureMode

        class DownAdapter:
            def query(self, selector: Any) -> Any:
                raise ConnectionError("ERP unreachable")

            def reserve(self, ref: str, intent_id: str) -> Any: ...
            def consume(self, ref: str, intent_id: str) -> Any: ...
            def release(self, ref: str, intent_id: str) -> Any: ...

        def run(resource: str, action: str, mode: FailureMode) -> Any:
            gctx = gate_ctx(
                resource, action, data=GOOD_DATA,
                obligations={PO_REGISTRY: DownAdapter()}, failure_mode=mode,
            )
            return g.require_match(MATCH_CFG, gctx)

        # closed (default): any outage denies.
        assert run("Payment", "pay", FailureMode.CLOSED).outcome is Outcome.FAIL
        # open + compensable effect: the outage is allowed through (§10).
        assert run("Payment", "pay", FailureMode.OPEN).outcome is Outcome.PASS
        # open + IRREVERSIBLE effect: the floor wins — MUST resolve closed.
        assert run("Email", "sendEmail", FailureMode.OPEN).outcome is Outcome.FAIL

    def test_require_match_is_volatile(self) -> None:
        assert "requireMatch" in VOLATILE_GATES


# ==========================================================================
# Engine + pipeline end-to-end (verification mode, holds, audit lineage)
# ==========================================================================
POLICY_DATA: dict[str, Any] = {
    "apiVersion": "stele/v0.1",
    "agent": "match-test-agent",
    "defaults": {"failureMode": "closed", "audit": "full"},
    "allow": [
        {"effect": ["pay"]},
        {"record": {"LedgerEntry": ["create"]}},
    ],
    "gates": {
        "pay": {"requireMatch": dict(MATCH_CFG)},
        "LedgerEntry.create": {
            "requireMatch": {
                **{k: v for k, v in MATCH_CFG.items() if k != "consume"},
                "consume": "none",  # pure verification on a record action
            }
        },
    },
}


@dataclass
class Harness:
    reg: Any
    policy: Any
    audit: InMemoryAuditSink
    outbox: InMemoryOutboxStore
    engine: DefaultGateEngine
    adapter: InMemoryObligationRegistry

    def enforce(
        self,
        resource: str = "Payment",
        action: str = "pay",
        data: dict[str, Any] | None = None,
    ) -> Any:
        return enforce(
            RawCall(resource=resource, action=action, data=data or dict(GOOD_DATA)),
            Actor(id="alice"),
            Session(id="s1", correlation_id="corr-1"),
            registry=self.reg,
            audit=self.audit,
            policy=self.policy,
            gates=self.engine,
            outbox=self.outbox,
            env=RequestEnv(now=T0),
            freshness=CFG_FRESHNESS,
            obligations={PO_REGISTRY: self.adapter},
        )

    def worker_at(self, now: datetime) -> DispatchWorker:
        from stonefold_core import Connectors

        conn = InMemoryConnector()
        connectors = Connectors({"email": conn, "sql": conn, "in_memory": conn})
        return DispatchWorker(
            self.outbox,
            connectors,
            registry=self.reg,
            clock=lambda: now,
            revalidate=make_dispatch_revalidator(self.engine, self.policy),
            obligations={PO_REGISTRY: self.adapter},
        )


def harness(
    policy_data: dict[str, Any] | None = None,
    records: dict[str, dict[str, Any]] | None = None,
    *,
    default_resolver_role: str | None = None,
) -> Harness:
    reg = full_registry()
    policy = load_policy(policy_data or POLICY_DATA, reg, schema=load_schema())
    adapter = InMemoryObligationRegistry(
        records if records is not None else {"po-1": PO_FIELDS}
    )
    engine = DefaultGateEngine(
        reg,
        obligations={PO_REGISTRY: adapter},
        default_resolver_role=default_resolver_role,
    )
    audit = InMemoryAuditSink()
    return Harness(reg, policy, audit, InMemoryOutboxStore(audit), engine, adapter)


class TestEndToEnd:
    def test_verification_only_record_action_allows_and_audits_lineage(self) -> None:
        h = harness()
        result = h.enforce("LedgerEntry", "create", dict(GOOD_DATA))
        assert result.decision is Decision.ALLOW
        rec = h.audit.records[-1]
        assert rec.obligationRefs == {
            "registry": PO_REGISTRY, "refs": ["po-1"], "candidates": 1,
        }

    def test_matched_effect_stages_with_lineage(self) -> None:
        h = harness()
        result = h.enforce()
        assert result.decision is Decision.ALLOW
        assert result.ticket is not None
        rec = h.audit.records[-1]
        assert rec.obligationRefs is not None and rec.obligationRefs["refs"] == ["po-1"]

    def test_no_match_deny_reaches_the_agent_as_terminal(self) -> None:
        h = harness(records={"po-1": {**PO_FIELDS, "state": "closed"}})
        result = h.enforce()
        assert result.decision is Decision.DENY
        assert result.reason_code == "no-match"
        assert result.retry_class is RetryClass.TERMINAL
        assert h.audit.records[-1].obligationRefs == {
            "registry": PO_REGISTRY, "refs": [], "candidates": 0,
        }

    def test_no_match_hold_stages_with_the_declared_resolver(self) -> None:
        data = dict(POLICY_DATA)
        data["gates"] = {
            "pay": {"requireMatch": {**MATCH_CFG, "onNoMatch": "hold"}},
        }
        h = harness(data, records={})
        result = h.enforce()
        assert result.decision is Decision.HOLD
        assert result.reason_code == "no-match"
        assert result.retry_class is None  # a gateway hold means WAIT (RFC §11)
        assert result.ticket is not None
        row = h.outbox.get(result.ticket)
        assert row is not None and row.state is PendingState.PENDING_APPROVAL
        assert len(row.releases) == 1
        contract = row.releases[0]
        assert contract.gate == "requireMatch"
        assert contract.approvers == ("role:ap-clerk",)
        assert contract.reason_code == "no-match"

    def test_hold_without_resolver_or_default_is_refused_unresolvable(self) -> None:
        data = dict(POLICY_DATA)
        cfg = {k: v for k, v in MATCH_CFG.items() if k != "resolvers"}
        data["gates"] = {"pay": {"requireMatch": {**cfg, "onNoMatch": "hold"}}}
        h = harness(data, records={})
        result = h.enforce()
        assert result.decision is Decision.DENY
        assert result.rule == "hold-unresolvable"
        assert result.retry_class is RetryClass.ESCALATE

    def test_ambiguous_never_relaxes_composition(self) -> None:
        # CS-032 rule 5: a matched obligation never relaxes another gate — a
        # failing valueLimit still short-circuits to DENY before any hold.
        data = dict(POLICY_DATA)
        data["gates"] = {
            "pay": {
                "valueLimit": {"field": "data.amount", "max": 100},
                "requireMatch": dict(MATCH_CFG),
            }
        }
        h = harness(data)
        result = h.enforce()
        assert result.decision is Decision.DENY
        assert result.rule == "gate:valueLimit"

    def test_dispatch_revalidation_cancels_when_the_reservation_is_lost(self) -> None:
        # CS-032 rule 3 / CS-035: for a row holding a reservation, the dispatch
        # claim checks reservation LIVENESS instead of re-running the query. A
        # reservation lost to another intent cancels stale-guard:requireMatch.
        h = harness()
        result = h.enforce()
        assert result.decision is Decision.ALLOW and result.ticket is not None
        row = h.outbox.get(result.ticket)
        assert row is not None and row.obligation is not None
        # simulate the loss: the adapter forgets the row's hold and another
        # intent takes the line before dispatch.
        h.adapter.state_of("po-1").reserved_by = None
        assert h.adapter.reserve("po-1", "someone-else") is not None
        h.worker_at(T0 + timedelta(minutes=1)).run_once()
        settled = h.outbox.get(result.ticket)
        assert settled is not None
        assert settled.state is PendingState.CANCELLED
        assert settled.reason == "stale-guard:requireMatch"
        rec = h.audit.records[-1]
        assert rec.consumption is not None and rec.consumption["state"] == "released"

    def test_agent_view_redacts_evidence_but_audit_keeps_lineage(self) -> None:
        from stonefold_core import agent_view

        h = harness(records={"po-1": {**PO_FIELDS, "state": "closed"}})
        result = h.enforce()
        view = agent_view(result)  # default code+fields
        match_gates = [gr for gr in view.gates if gr.gate == "requireMatch"]
        assert match_gates and match_gates[0].evidence is None
        assert match_gates[0].code == "no-match"
        assert h.audit.records[-1].obligationRefs is not None


# ==========================================================================
# CS-040 (v0.6.1) — the hold dedupe identity is the QUESTION, not the code
# ==========================================================================
class TestDedupeSharpness:
    def _pay(self, h: Harness, vendor: str, amount: float, session: str) -> Any:
        return enforce(
            RawCall(resource="Payment", action="pay",
                    data={**GOOD_DATA, "vendorId": vendor, "amount": amount}),
            Actor(id="alice"),
            Session(id=session, correlation_id=session),
            registry=h.reg, audit=h.audit, policy=h.policy, gates=h.engine,
            outbox=h.outbox, env=RequestEnv(now=T0), freshness=CFG_FRESHNESS,
            obligations={PO_REGISTRY: h.adapter},
            dedupe_window_s=3600.0,
        )

    def test_distinct_unmatched_intents_never_collapse(self) -> None:
        # v0.6's key over-collapsed here: with zero candidates the refs are
        # empty, so every no-match hold on one action shared a key. CS-040
        # identifies a zero-candidate hold by what the intent CLAIMED — its
        # compared field values — so two different vendors' unmatched invoices
        # are two questions, while resubmitting the SAME one still collapses.
        data = dict(POLICY_DATA)
        data["gates"] = {"pay": {"requireMatch": {**MATCH_CFG, "onNoMatch": "hold"}}}
        h = harness(data, records={})

        first = self._pay(h, "V-77", 800, "s1")
        assert first.decision is Decision.HOLD and first.reason_code == "no-match"

        other_vendor = self._pay(h, "V-99", 800, "s2")
        assert other_vendor.decision is Decision.HOLD
        assert other_vendor.ticket != first.ticket  # a DIFFERENT question

        other_amount = self._pay(h, "V-77", 990, "s3")
        assert other_amount.ticket != first.ticket  # a different claim

        same_again = self._pay(h, "V-77", 800, "s4")
        assert same_again.ticket == first.ticket  # the same question collapses
        row = h.outbox.get(first.ticket)
        assert row is not None and row.attempts == 2

        assert len(h.outbox.list_by_state(PendingState.PENDING_APPROVAL)) == 3


# ==========================================================================
# CS-038 — linter rules 14–17 and the rule-4 amendment
# ==========================================================================
def _lint_policy(gates: dict[str, Any], *, allow: list[dict[str, Any]] | None = None) -> Any:
    policy = Policy.model_validate(
        {
            "agent": "lint-agent",
            "allow": allow or [{"effect": ["pay"]}],
            "gates": gates,
        }
    )
    return lint(policy, full_registry())


def _codes(report: Any, severity: Severity) -> list[str]:
    return [f.code for f in report.findings if f.severity is severity]


class TestLinterRules:
    def test_rule14_unknown_registry_is_an_error(self) -> None:
        report = _lint_policy({"pay": {"requireMatch": {**MATCH_CFG, "registry": "nope"}}})
        assert "13.14" in _codes(report, Severity.ERROR)

    def test_rule14_undeclared_obligation_path_is_an_error(self) -> None:
        cfg = {**MATCH_CFG, "match": ["obligation.ghostField == data.vendorId"]}
        report = _lint_policy({"pay": {"requireMatch": cfg}})
        assert any(
            "ghostField" in f.message
            for f in report.errors
            if f.code == "13.14"
        )

    def test_rule14_tolerance_on_non_numeric_field_is_an_error(self) -> None:
        cfg = {
            **MATCH_CFG,
            "match": [
                {"field": "obligation.state", "matches": "data.amount", "within": "10%"}
            ],
        }
        report = _lint_policy({"pay": {"requireMatch": cfg}})
        assert any(
            "numeric" in f.message for f in report.errors if f.code == "13.14"
        )

    def test_rule14_valid_config_produces_no_errors(self) -> None:
        report = _lint_policy({"pay": {"requireMatch": dict(MATCH_CFG)}})
        assert not [f for f in report.errors if f.code.startswith("13.1")]

    def test_rule17_on_ambiguous_allow_is_an_error(self) -> None:
        report = _lint_policy(
            {"pay": {"requireMatch": {**MATCH_CFG, "onAmbiguous": "allow"}}}
        )
        assert "13.17" in _codes(report, Severity.ERROR)

    def test_rule16_consume_none_on_irreversible_effect_warns(self) -> None:
        cfg = {**MATCH_CFG, "consume": "none"}
        report = _lint_policy(
            {"sendEmail": {"requireMatch": cfg}},
            allow=[{"effect": ["sendEmail"]}],
        )
        assert "13.16" in _codes(report, Severity.WARN)

    def test_rule15_visible_overlap_is_an_error(self) -> None:
        # A registry whose adapter connector also backs a resource the policy
        # may write: the agent could author its own obligations.
        reg_data = {
            "connectors": ["erp-po-adapter"],
            "obligationRegistries": {
                PO_REGISTRY: {
                    "connector": "erp-po-adapter",
                    "capability": "transactional",
                    "schema": {
                        "vendorId": {"type": "string"},
                        "state": {"values": ["open", "closed"]},
                        "vendor": {"properties": {"domain": {"type": "string"}}},
                        "line": {
                            "properties": {
                                "amount": {"type": "decimal"},
                                "state": {"values": ["unconsumed", "consumed"]},
                            }
                        },
                    },
                }
            },
            "resources": {
                "PurchaseOrder": {
                    "connector": "erp-po-adapter",
                    "actions": {"createOrder": {"kind": "record"}},
                },
                "Payment": {
                    "connector": "erp-po-adapter",
                    "actions": {"pay": {"kind": "effect"}},
                },
            },
        }
        registry = load_registry(reg_data)
        policy = Policy.model_validate(
            {
                "agent": "self-dealing-agent",
                "allow": [{"record": {"PurchaseOrder": ["createOrder"]}}, {"effect": ["pay"]}],
                "gates": {"pay": {"requireMatch": dict(MATCH_CFG)}},
            }
        )
        report = lint(policy, registry)
        assert any(
            "author its own obligations" in f.message
            for f in report.errors
            if f.code == "13.15"
        )

    def test_rule15_external_registry_emits_the_deployment_info(self) -> None:
        report = _lint_policy({"pay": {"requireMatch": dict(MATCH_CFG)}})
        assert "13.15" in _codes(report, Severity.INFO)
        assert "13.15" not in _codes(report, Severity.ERROR)

    def test_rule4_amended_require_match_guards_an_irreversible(self) -> None:
        gates = {"sendEmail": {"requireMatch": dict(MATCH_CFG)}}
        report = _lint_policy(gates, allow=[{"effect": ["sendEmail"]}])
        assert "13.4" not in [f.code for f in report.findings]
        bare = _lint_policy({}, allow=[{"effect": ["sendEmail"]}])
        assert "13.4" in [f.code for f in bare.findings]

    def test_example_fixtures_lint_clean_under_the_new_rules(self) -> None:
        import yaml
        from tests.conftest import EXAMPLES

        for name in ("payments-ops.stele.yaml", "ward-nurse.stele.yaml"):
            with (EXAMPLES / name).open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
            report = validate_only(data, full_registry(), schema=load_schema())
            assert not report.has_errors, f"{name}: {report.format()}"
