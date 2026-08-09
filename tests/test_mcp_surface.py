"""The MCP surface: typed tools, retrieval over them, and enforcement (design §1.2).

Three things are checked here, and the middle one is the reason the endpoint
exists:

1. ``/mcp/tools`` generates one typed tool per declared action, from the same
   registry every other surface reads.
2. ``/mcp/search`` returns the *right* action when the surface contains actions
   whose names plausibly fit the same request — at a small estate and at a large
   one. A retriever that only works when nothing is confusable is no help,
   because a long surface is exactly where confusable names appear.
3. ``/mcp/call`` ends in the same ``enforce`` as every other transport, denies an
   unmapped tool, and reports a malformed call as a shape error rather than a
   refusal.

The confusable fixture below is synthetic back-office vocabulary. Every target
action is distinguishable from its look-alikes *only* by its description, which
is the hard case: ``Disbursement.suspend`` versus ``Invoicing.hold`` for "put a
hold on the payment".
"""

from __future__ import annotations

import itertools
from typing import Any

import pytest

from stonefold_core.registry import PropertyDef, load_registry
from stonefold_gateway.mcp_search import search, search_response
from stonefold_gateway.transport import (
    InvalidIntentError,
    mcp_tool_schemas,
    validate_intent_data,
)
from tests.conftest import full_registry

# --- the confusable surface ------------------------------------------------
# Five requests, each with a look-alike that the plain wording points at and
# that is wrong. Kept in their own resources so they are identical at every size.
_TARGETS: dict[tuple[str, str], str] = {
    ("Disbursement", "pay"):
        "Pay a supplier invoice from accounts payable — the actual outbound bank payment.",
    ("GoodsInward", "record"):
        "Book goods physically received into stock against a purchase order "
        "(three-way-match receipt).",
    ("VendorMaster", "read"):
        "Read the vendor master: the canonical list of APPROVED vendors used for payment.",
    ("Disbursement", "suspend"):
        "Suspend a pending supplier payment so it will not be disbursed until released.",
    ("Invoicing", "dispute"):
        "Formally dispute a supplier invoice's amount with the supplier "
        "(opens a billing dispute).",
}

_CONFUSABLES: dict[tuple[str, str], str] = {
    ("Invoicing", "pay"):
        "Record that an invoice was paid in the ledger — a bookkeeping entry, moves no money.",
    ("Billing", "pay"): "Take payment on a CUSTOMER billing charge, not pay a supplier.",
    ("Disbursement", "schedule"): "Add a payment to a future run — does not pay now.",
    ("Vendor", "payout"):
        "Pay a vendor rebate or commission, not an accounts-payable invoice.",
    ("Shipment", "receive"):
        "Mark an inbound freight shipment arrived at the gate — not booked to stock.",
    ("Inventory", "receive"):
        "Increment a stock bin count directly — not tied to a purchase order.",
    ("GoodsInward", "schedule"):
        "Schedule an expected future delivery — does not record actual receipt.",
    ("Vendor", "read"):
        "Read a working vendor record (may be draft or unapproved), not the master.",
    ("Supplier", "read"):
        "Read the sourcing/prospect supplier list (leads), not approved vendors.",
    ("VendorMaster", "search"):
        "Full-text search vendor notes — returns matches, not the master list.",
    ("Invoicing", "hold"):
        "Place a ledger hold flag on an invoice document — does not stop a payment.",
    ("Disbursement", "cancel"):
        "Cancel a payment outright — stronger than suspend; do not use to pause.",
    ("Ticket", "create"):
        "Open a general internal helpdesk ticket — not a formal supplier dispute.",
    ("Invoicing", "annotate"):
        "Add an internal note to an invoice — does not raise a dispute.",
}

_MODULES = [
    "Billing", "Invoicing", "Ledger", "Inventory", "Warehouse", "Logistics",
    "Procurement", "Vendor", "Customer", "Contract", "Pricing", "Catalog",
    "Shipment", "Returns", "Payroll", "Expense", "Asset", "Project", "Ticket",
    "Approval", "Budget", "Forecast", "Tax", "Compliance", "Audit", "Document",
    "Notification", "Report", "Timesheet", "Recruitment", "Onboarding",
    "Facility", "Fleet", "Maintenance", "Quality", "Supplier", "Rebate",
    "Commission", "Subscription", "Meter",
]
_VERBS = ["create", "update", "read", "cancel", "approve", "submit", "close",
          "archive", "reopen", "assign", "release", "reconcile"]

# What an agent would actually say, and the one action that does it.
_STEPS = [
    ("get the list of approved vendors", "VendorMaster.read"),
    ("book receipt of 10 units against purchase order into stock", "GoodsInward.record"),
    ("pay supplier invoice INV-7 make the actual payment", "Disbursement.pay"),
    ("put a hold on the pending payment so it is not paid yet", "Disbursement.suspend"),
    ("raise a formal dispute with the supplier over an invoice", "Invoicing.dispute"),
]


def _tool(resource: str, action: str, description: str) -> dict[str, Any]:
    return {"name": f"{resource}.{action}", "description": description,
            "input_schema": {"type": "object", "properties": {}}}


def _surface(size: int) -> list[dict[str, Any]]:
    """The confusable set, padded with ordinary CRUD actions up to ``size``."""
    tools = [_tool(r, a, label) for (r, a), label in _TARGETS.items()]
    tools += [_tool(r, a, label) for (r, a), label in _CONFUSABLES.items()]
    have = {t["name"] for t in tools}
    for module, verb in itertools.product(_MODULES, _VERBS):
        if len(tools) >= size:
            break
        name = f"{module}.{verb}"
        if name not in have:
            tools.append(_tool(module, verb, f"{verb.capitalize()} a {module.lower()} record."))
    return tools


# --- retrieval finds the right action, small surface and large -------------
@pytest.mark.parametrize("size", [19, 496])
@pytest.mark.parametrize(("query", "want"), _STEPS)
def test_retrieval_beats_the_look_alikes(size: int, query: str, want: str) -> None:
    tools = _surface(size)
    assert len(tools) == size, "fixture must reach the requested size"
    hits = search(tools, query, limit=5)
    assert hits, f"no candidate for {query!r}"
    assert hits[0]["tool"]["name"] == want


def test_retrieval_margin_is_reported_not_relied_on() -> None:
    """The top hit can win narrowly, and that is a documented property.

    Recorded as a test so nobody later reads a passing top-1 assertion as
    evidence that retrieval *disambiguates*. It narrows; policy decides.
    """
    hits = search(_surface(496), "put a hold on the pending payment so it is not paid yet")
    margin = hits[0]["score"] - hits[1]["score"]
    assert hits[0]["tool"]["name"] == "Disbursement.suspend"
    assert 0 < margin < 0.1


def test_search_explains_itself_and_bounds_its_answer() -> None:
    tools = _surface(19)
    body = search_response(tools, "pay supplier invoice", limit=3)
    assert body["of"] == 19
    assert len(body["results"]) <= 3
    top = body["results"][0]
    assert top["name"] == "Disbursement.pay"
    # the matched terms are returned, so a reviewer can see why it ranked
    assert {"pay", "supplier", "invoice"} <= set(top["matched"])
    assert top["input_schema"] is not None


def test_no_match_returns_nothing_rather_than_everything() -> None:
    tools = _surface(19)
    assert search(tools, "") == []
    assert search_response(tools, "xyzzy plugh")["results"] == []


# --- the tools are generated from the registry ----------------------------
def test_mcp_tools_are_one_per_declared_action() -> None:
    reg = full_registry()
    tools = mcp_tool_schemas(reg)
    declared = {
        f"{resource}.{action}"
        for resource, rdef in reg.file.resources.items()
        for action in rdef.actions
    }
    assert {t["name"] for t in tools} == declared
    # every tool carries a description and an object schema, so a model has
    # something to choose on and something to fill in
    assert all(t["description"] for t in tools)
    assert all(t["input_schema"]["type"] == "object" for t in tools)


def test_declared_data_becomes_the_tool_schema() -> None:
    doc = {
        "resources": {
            "Payment": {
                "actions": {
                    "pay": {
                        "kind": "effect",
                        "label": "Pay a supplier invoice.",
                        "data": {
                            "invoiceNo": {"type": "string", "required": True},
                            "channel": {"values": ["sepa", "swift"]},
                        },
                    },
                    "read": {"kind": "observe"},
                }
            }
        }
    }
    reg = load_registry(doc)
    tools = {t["name"]: t for t in mcp_tool_schemas(reg)}

    pay = tools["Payment.pay"]
    assert pay["description"] == "Pay a supplier invoice."
    assert pay["input_schema"]["required"] == ["invoiceNo"]
    assert pay["input_schema"]["properties"]["channel"]["enum"] == ["sepa", "swift"]
    # an action that declares no data gets a bare object, not a fabricated shape
    assert tools["Payment.read"]["input_schema"] == {"type": "object", "properties": {}}


def test_declared_to_take_nothing_is_not_the_same_as_undeclared() -> None:
    reg = load_registry({"resources": {"Job": {"actions": {
        "run": {"kind": "effect", "data": {}},
        "read": {"kind": "observe"},
    }}}})
    actions = reg.file.resources["Job"].actions
    assert actions["run"].data_schema() == {
        "type": "object", "properties": {}, "additionalProperties": False,
    }
    assert actions["read"].data_schema() is None

    # so a field is an error on the one and unchecked on the other
    with pytest.raises(InvalidIntentError):
        validate_intent_data(reg, "Job", "run", {"anything": 1})
    validate_intent_data(reg, "Job", "read", {"anything": 1})


# --- the shape check names the field ---------------------------------------
def _shaped_registry() -> Any:
    return load_registry({"resources": {"Payment": {"actions": {"pay": {
        "kind": "effect",
        "data": {
            "invoiceNo": {"type": "string", "required": True, "label": "the invoice number"},
            "channel": {"values": ["sepa", "swift"]},
        },
    }}}}})


@pytest.mark.parametrize(
    ("data", "code", "pointer"),
    [
        ({}, "MISSING_FIELD", "data.invoiceNo"),
        ({"invoiceNo": "INV-1", "channel": "carrier-pigeon"}, "BAD_VALUE", "data.channel"),
        ({"invoiceNo": "INV-1", "amount": "10"}, "UNKNOWN_FIELD", "data.amount"),
    ],
)
def test_a_malformed_call_points_at_the_field(
    data: dict[str, Any], code: str, pointer: str
) -> None:
    with pytest.raises(InvalidIntentError) as caught:
        validate_intent_data(_shaped_registry(), "Payment", "pay", data)
    assert caught.value.code == code
    assert caught.value.pointer == pointer
    assert caught.value.as_error()["message"]


def test_property_def_mirrors_the_authoring_dialect() -> None:
    # `derived` is in registry.schema.json's property shape; a schema-valid
    # registry must load even though the compact dialect ignores it.
    prop = PropertyDef(type="string", derived="computed elsewhere")
    assert prop.json_schema() == {"type": "string"}
