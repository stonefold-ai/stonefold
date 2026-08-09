"""v0.6.1 CS-043 — per-item verdicts for item-bearing actions (RFC §12).

The failure being closed: one action carries twenty worklist items, thirteen of
which need a human. The gateway can allow the call or refuse it, so refusing it
takes the seven legitimate items down with the thirteen — an outage, not a
control. Denying the call and controlling the call are the same act.

What is checked here: the fan-out happens only where it is declared, the applied
subset is one call, refusals name their item, held items stage individually, the
envelope cannot be misread as success, and a batch is still atomic.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from stonefold_core import (
    Actor,
    Connectors,
    Decision,
    InMemoryAuditSink,
    RawCall,
    Session,
    enforce,
    enforce_batch,
    load_policy,
)
from stonefold_core.gating import RequestEnv
from stonefold_core.linter import Severity, lint
from stonefold_core.policy import Policy
from stonefold_core.registry import load_registry
from stonefold_connectors import InMemoryConnector
from stonefold_gates.base import GateContext, check_fail, check_hold, check_pass
from stonefold_gates.engine import DefaultGateEngine
from stonefold_store import InMemoryOutboxStore
from tests.conftest import load_schema

ACTOR = Actor(id="agent-7")
RUN = Session(id="s1", correlation_id="run-1")

REGISTRY: dict[str, Any] = {
    "resources": {
        "WorklistItem": {
            "actions": {
                "markManyReviewed": {
                    "kind": "record",
                    "label": "Close several worklist items at once.",
                    "data": {"itemIds": {"type": "array", "required": True}},
                    "items": {"field": "itemIds", "independent": True},
                },
                # the same action, declared as a set but NOT independent
                "archiveMany": {
                    "kind": "record",
                    "data": {"itemIds": {"type": "array", "required": True}},
                    "items": {"field": "itemIds"},
                },
            }
        },
        "Transfer": {
            "actions": {
                # two legs, and applying one is worse than applying neither: the
                # declaration says so by leaving `independent` off
                "post": {
                    "kind": "effect",
                    "data": {"legs": {"type": "array", "required": True}},
                    "items": {"field": "legs"},
                }
            }
        },
    },
    "preconditionChecks": [
        {"name": "itemIsClean", "holdCapable": True,
         "reasonCodes": {"ITEM_NEEDS_CLINICIAN": "terminal", "ITEM_AMBIGUOUS": "escalate"}},
    ],
}

POLICY: dict[str, Any] = {
    "agent": "worklist",
    "allow": [{"record": ["markManyReviewed", "archiveMany"]}, {"effect": ["post"]}],
    "gates": {
        "markManyReviewed": {
            "precondition": {"checks": ["itemIsClean"], "resolvers": "role:supervisor"}
        }
    },
}

# Items 13 and 14 are the ones a clinician must see; 15 is judgment-shaped and
# holds. Everything else is absorbable.
_NEEDS_CLINICIAN = {"W-13", "W-14"}
_AMBIGUOUS = {"W-15"}


def _item_is_clean(gctx: GateContext) -> Any:
    ids = gctx.resolved.data.get("itemIds") or []
    # the check sees ONE item, which is the point of the fan-out
    assert len(ids) == 1, f"the check saw {len(ids)} items; the fan-out did not happen"
    item = str(ids[0])
    if item in _NEEDS_CLINICIAN:
        return check_fail("ITEM_NEEDS_CLINICIAN")
    if item in _AMBIGUOUS:
        return check_hold("ITEM_AMBIGUOUS", {"item": item})
    return check_pass()


def _world() -> dict[str, Any]:
    reg = load_registry(REGISTRY)
    audit = InMemoryAuditSink()
    return {
        "registry": reg,
        "audit": audit,
        "policy": load_policy(POLICY, reg, schema=load_schema()),
        "gates": DefaultGateEngine(reg, preconditions={"itemIsClean": _item_is_clean}),
        "outbox": InMemoryOutboxStore(audit=audit),
        "connectors": Connectors({"in_memory": InMemoryConnector()}),
    }


def _mark(*items: str, action: str = "markManyReviewed") -> RawCall:
    return RawCall(resource="WorklistItem", action=action, data={"itemIds": list(items)})


# --- the fan-out, and what it buys ----------------------------------------
def test_the_clean_items_are_applied_and_the_rest_are_not() -> None:
    """The whole finding, as one assertion: these three, not that one."""
    world = _world()
    result = enforce(_mark("W-1", "W-2", "W-13", "W-3"), ACTOR, RUN, **world)

    assert result.applied == ("W-1", "W-2", "W-3")
    assert [(v.item, v.decision) for v in result.items] == [
        ("W-1", Decision.ALLOW),
        ("W-2", Decision.ALLOW),
        ("W-13", Decision.DENY),
        ("W-3", Decision.ALLOW),
    ]
    refused = next(v for v in result.items if v.decision is Decision.DENY)
    assert refused.reason_code == "ITEM_NEEDS_CLINICIAN"
    assert refused.retry_class is not None and refused.retry_class.value == "terminal"


def test_the_envelope_cannot_be_read_as_success() -> None:
    """A partial application does not report `allow`.

    The alternative — `allow`, with the refusals in a list an actor may not read
    — is the mistake CS-029 exists to prevent, one level up.
    """
    result = enforce(_mark("W-1", "W-13"), ACTOR, RUN, **_world())
    assert result.decision is Decision.DENY
    assert result.applied == ("W-1",)
    assert result.partially_applied() is True
    assert result.reason_code == "ITEM_NEEDS_CLINICIAN"


def test_every_item_clean_is_a_plain_allow() -> None:
    result = enforce(_mark("W-1", "W-2", "W-3"), ACTOR, RUN, **_world())
    assert result.decision is Decision.ALLOW
    assert result.applied == ("W-1", "W-2", "W-3")
    assert result.partially_applied() is False


def test_every_item_refused_applies_nothing() -> None:
    result = enforce(_mark("W-13", "W-14"), ACTOR, RUN, **_world())
    assert result.decision is Decision.DENY
    assert result.applied == ()
    assert result.partially_applied() is False  # nothing was applied: not partial


# --- held items stage on their own ----------------------------------------
def test_a_held_item_gets_its_own_ticket_and_the_others_still_apply() -> None:
    """A human releases one item, not the call — otherwise the hold is an outage
    wearing a different hat."""
    result = enforce(_mark("W-1", "W-15", "W-2"), ACTOR, RUN, **_world())

    held = next(v for v in result.items if v.decision is Decision.HOLD)
    assert held.item == "W-15"
    assert held.reason_code == "ITEM_AMBIGUOUS"
    assert held.ticket, "a held item must be releasable on its own"
    assert result.applied == ("W-1", "W-2")
    # hold is less final than deny, so with no deny the envelope is the hold
    assert result.decision is Decision.HOLD


def test_a_deny_outranks_a_hold_in_the_envelope() -> None:
    result = enforce(_mark("W-15", "W-13"), ACTOR, RUN, **_world())
    assert result.decision is Decision.DENY  # the item that will never happen wins


# --- the applied subset is ONE call ---------------------------------------
def test_the_applied_items_go_through_as_one_call() -> None:
    """One transaction in the managed system, not N — and the record says which
    items it carried."""
    world = _world()
    enforce(_mark("W-1", "W-2", "W-13"), ACTOR, RUN, **world)
    audit = world["audit"]

    applied_records = [
        r for r in audit.records
        if r.action == "markManyReviewed" and r.decision is Decision.ALLOW
    ]
    assert len(applied_records) == 1
    assert applied_records[0].parameters.get("itemIds") == ["W-1", "W-2"]


def test_each_refused_item_is_audited_on_its_own() -> None:
    """The refusal's record is the only place the refusal exists."""
    world = _world()
    enforce(_mark("W-1", "W-13", "W-14"), ACTOR, RUN, **world)
    refusals = [r for r in world["audit"].records if r.decision is Decision.DENY]
    assert len(refusals) == 2
    assert {tuple(r.parameters["itemIds"])[0] for r in refusals} == {"W-13", "W-14"}


# --- where the fan-out must NOT happen ------------------------------------
def test_an_action_declared_not_independent_stays_all_or_nothing() -> None:
    """`independent` is a safety property: without it, a subset is not a
    meaningful outcome and the call keeps its old behaviour."""
    result = enforce(_mark("W-1", "W-13", action="archiveMany"), ACTOR, RUN, **_world())
    assert result.items == ()
    assert result.applied == ()
    assert result.decision is Decision.ALLOW  # archiveMany has no per-item gate


def test_both_legs_of_a_transfer_are_one_unit() -> None:
    world = _world()
    result = enforce(
        RawCall(resource="Transfer", action="post",
                data={"legs": [{"account": "A", "amount": 100}, {"account": "B", "amount": -100}]}),
        ACTOR, RUN, **world,
    )
    assert result.items == ()  # never half a transfer


def test_a_single_item_is_not_fanned_out() -> None:
    """Nothing to partition, so nothing gains from N records."""
    result = enforce(_mark("W-1"), ACTOR, RUN, **_world())
    assert result.items == ()
    assert result.decision is Decision.ALLOW


def test_a_batch_is_still_atomic() -> None:
    """CS-023 is unchanged: inside a batch an item-bearing action is one unit, so
    a refused item refuses the whole batch rather than partially applying."""
    world = _world()
    batch = enforce_batch(
        [
            RawCall(resource="WorklistItem", action="markManyReviewed",
                    data={"itemIds": ["W-1", "W-13"]}),
        ],
        ACTOR, RUN, **world,
    )
    assert batch.decision is Decision.DENY
    assert batch.results[0].items == ()  # decided as one unit, not fanned out


# --- the ceiling ----------------------------------------------------------
def test_a_call_over_the_declared_ceiling_is_refused_outright() -> None:
    registry = {
        "resources": {"WorklistItem": {"actions": {"markManyReviewed": {
            "kind": "record",
            "data": {"itemIds": {"type": "array", "required": True}},
            "items": {"field": "itemIds", "independent": True, "maxItems": 3},
        }}}},
    }
    reg = load_registry(registry)
    audit = InMemoryAuditSink()
    result = enforce(
        _mark("W-1", "W-2", "W-3", "W-4"), ACTOR, RUN,
        registry=reg, audit=audit,
        policy=load_policy({"agent": "w", "allow": [{"record": ["markManyReviewed"]}]},
                           reg, schema=load_schema()),
        gates=DefaultGateEngine(reg), outbox=InMemoryOutboxStore(audit=audit),
        connectors=Connectors({"in_memory": InMemoryConnector()}),
    )
    assert result.decision is Decision.DENY
    assert result.rule == "items-over-ceiling"
    assert result.applied == ()
    # refusing on size is one decision, so one record
    assert len(audit.records) == 1


# --- naming an item that is an object ------------------------------------
def test_an_object_item_is_named_by_its_declared_key() -> None:
    registry = {
        "resources": {"Disbursement": {"actions": {"payBatch": {
            "kind": "effect",
            "data": {"payments": {"type": "array", "required": True}},
            "items": {"field": "payments", "independent": True, "key": "invoiceNo"},
        }}}},
        "preconditionChecks": [
            {"name": "invoiceIsMatched", "holdCapable": False,
             "reasonCodes": {"NO_OPEN_ORDER": "terminal"}}
        ],
    }
    reg = load_registry(registry)
    audit = InMemoryAuditSink()

    def matched(gctx: GateContext) -> Any:
        payments = gctx.resolved.data.get("payments") or []
        return check_pass() if payments[0]["invoiceNo"] != "INV-9" else check_fail("NO_OPEN_ORDER")

    result = enforce(
        RawCall(resource="Disbursement", action="payBatch", data={"payments": [
            {"invoiceNo": "INV-1", "amount": 100},
            {"invoiceNo": "INV-9", "amount": 900},
        ]}),
        ACTOR, RUN,
        registry=reg, audit=audit,
        policy=load_policy(
            {"agent": "pay", "allow": [{"effect": ["payBatch"]}],
             "gates": {"payBatch": {"precondition": ["invoiceIsMatched"]}}},
            reg, schema=load_schema()),
        gates=DefaultGateEngine(reg, preconditions={"invoiceIsMatched": matched}),
        outbox=InMemoryOutboxStore(audit=audit),
        connectors=Connectors({"in_memory": InMemoryConnector()}),
    )
    assert [(v.item, v.decision) for v in result.items] == [
        ("INV-1", Decision.ALLOW),
        ("INV-9", Decision.DENY),
    ]
    assert result.applied == ("INV-1",)


# --- the declaration ------------------------------------------------------
def test_fans_out_requires_both_halves() -> None:
    reg = load_registry(REGISTRY)
    actions = reg.file.resources["WorklistItem"].actions
    assert actions["markManyReviewed"].fans_out() is True
    assert actions["archiveMany"].fans_out() is False  # declared, not independent


@pytest.mark.parametrize("count", [2, 5, 12])
def test_every_item_gets_a_verdict(count: int) -> None:
    items = [f"W-{i}" for i in range(1, count + 1)]
    result = enforce(_mark(*items), ACTOR, RUN, **_world())
    assert [v.item for v in result.items] == items


# --- what the fan-out does to aggregate gates -----------------------------
def test_an_aggregate_gate_cuts_the_run_off_mid_call() -> None:
    """The side effect worth knowing about, and it took a wrong test to see it.

    An aggregate gate on a bundled action used to see ONE increment per call, so
    ``rate: 2/day`` did not bound a call carrying fifty items. Per item it sees N
    and the run stops where the allowance does — the same "these two, not those
    four" shape, reached from a completely different gate.

    (Written first without an injected clock, which fail-closed on all four items
    and looked like the design failing. It was the test failing. The clock is not
    optional for a time-based gate — invariant 1: we never invent a time.)
    """
    reg = load_registry({"resources": {"WorklistItem": {"actions": {"markManyReviewed": {
        "kind": "record",
        "data": {"itemIds": {"type": "array", "required": True}},
        "items": {"field": "itemIds", "independent": True},
    }}}}})
    audit = InMemoryAuditSink()
    result = enforce(
        _mark("W-1", "W-2", "W-3", "W-4"), ACTOR, RUN,
        registry=reg, audit=audit,
        env=RequestEnv(now=datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc)),
        policy=load_policy(
            {"agent": "w", "allow": [{"record": ["markManyReviewed"]}],
             "gates": {"markManyReviewed": {"rate": "2/day"}}},
            reg, schema=load_schema()),
        gates=DefaultGateEngine(reg), outbox=InMemoryOutboxStore(audit=audit),
        connectors=Connectors({"in_memory": InMemoryConnector()}),
    )
    assert result.applied == ("W-1", "W-2")
    assert [v.decision for v in result.items[2:]] == [Decision.DENY, Decision.DENY]
    assert result.decision is Decision.DENY


def test_a_time_based_gate_with_no_clock_refuses_every_item() -> None:
    """Fail-closed survives the fan-out: no clock means no item passes, and it
    means it visibly rather than by allowing the ones evaluated before the
    allowance ran out."""
    reg = load_registry({"resources": {"WorklistItem": {"actions": {"markManyReviewed": {
        "kind": "record",
        "data": {"itemIds": {"type": "array", "required": True}},
        "items": {"field": "itemIds", "independent": True},
    }}}}})
    audit = InMemoryAuditSink()
    result = enforce(
        _mark("W-1", "W-2"), ACTOR, RUN, registry=reg, audit=audit,
        policy=load_policy(
            {"agent": "w", "allow": [{"record": ["markManyReviewed"]}],
             "gates": {"markManyReviewed": {"rate": "2/day"}}},
            reg, schema=load_schema()),
        gates=DefaultGateEngine(reg), outbox=InMemoryOutboxStore(audit=audit),
        connectors=Connectors({"in_memory": InMemoryConnector()}),
    )
    assert result.applied == ()
    assert all(v.decision is Decision.DENY for v in result.items)


# --- §13.21, both directions ----------------------------------------------
def test_lint_refuses_an_items_field_that_is_not_declared() -> None:
    """A misspelled field is silent at runtime: the call never fans out, so the
    gate the author thought they wrote does not exist."""
    reg = load_registry({"resources": {"W": {"actions": {"markMany": {
        "kind": "record",
        "data": {"itemIds": {"type": "array"}},
        "items": {"field": "itemIDs", "independent": True, "maxItems": 50},
    }}}}})
    report = lint(Policy.model_validate({"agent": "w", "allow": [{"record": ["markMany"]}]}), reg)
    rule = [f for f in report.findings if f.code == "13.21"]
    assert rule and rule[0].severity is Severity.ERROR


def test_lint_warns_about_an_unbounded_fan_out() -> None:
    reg = load_registry(REGISTRY)
    report = lint(Policy.model_validate(POLICY), reg)
    warns = [f for f in report.findings if f.code == "13.21" and f.severity is Severity.WARN]
    assert warns and "maxItems" in warns[0].message


def test_a_declared_ceiling_silences_the_warning() -> None:
    reg = load_registry({"resources": {"W": {"actions": {"markMany": {
        "kind": "record",
        "data": {"itemIds": {"type": "array"}},
        "items": {"field": "itemIds", "independent": True, "maxItems": 200},
    }}}}})
    report = lint(Policy.model_validate({"agent": "w", "allow": [{"record": ["markMany"]}]}), reg)
    assert [f for f in report.findings if f.code == "13.21"] == []
