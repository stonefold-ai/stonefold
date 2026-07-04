"""stonefold_registry_gen — the authoring-time registry drafting tool.

The generator turns what an integrator already has (SQL DDL, an OpenAPI spec,
an MCP tool list) into a DRAFT registry in the v1.x authoring format
(docs/06, schema/registry.schema.json). Every guessed kind/attribute is marked
``TODO(review)`` — a human fixes and signs the result; nothing generated here
ever sits in the enforcement path.

Contract under test:
1. every emitted draft is valid YAML and validates against
   schema/registry.schema.json;
2. kinds are guessed from verb heuristics and flagged when uncertain;
3. dangerous-looking verbs get a suggested reversibility for review;
4. the draft carries TODO(review) markers (it must not look finished).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from stonefold_registry_gen import (
    draft_from_mcp_tools,
    draft_from_openapi,
    draft_from_sql,
    emit_yaml,
    validate_registry_yaml,
)
from stonefold_registry_gen.kinds import guess_kind, pascal, singular, split_words, suggest_reversibility


# --------------------------------------------------------------------------
# heuristics
# --------------------------------------------------------------------------
def test_split_words_handles_snake_kebab_camel() -> None:
    assert split_words("send_email") == ["send", "email"]
    assert split_words("send-email") == ["send", "email"]
    assert split_words("sendEmail") == ["send", "Email"]
    assert split_words("getCustomerOrders") == ["get", "Customer", "Orders"]


def test_singular_and_pascal() -> None:
    assert singular("payments") == "payment"
    assert singular("statuses") == "status"
    assert singular("entries") == "entry"
    assert singular("address") == "address"  # trailing 'ss' untouched
    assert pascal(["customer", "orders"]) == "CustomerOrders"


def test_guess_kind_verb_tables() -> None:
    assert guess_kind("get") == ("observe", True)
    assert guess_kind("list") == ("observe", True)
    assert guess_kind("create") == ("record", True)
    assert guess_kind("delete") == ("record", True)
    assert guess_kind("send") == ("effect", True)
    assert guess_kind("pay") == ("effect", True)
    assert guess_kind("approve") == ("transition", True)
    assert guess_kind("classify") == ("assess", True)


def test_guess_kind_unknown_defaults_to_effect_uncertain() -> None:
    # unknown verbs draft as `effect` (the most-gated kind — conservative) and
    # are flagged uncertain so the TODO marker is emitted.
    kind, certain = guess_kind("frobnicate")
    assert kind == "effect"
    assert certain is False


def test_suggest_reversibility_hints() -> None:
    assert suggest_reversibility("send") == "irreversible"
    assert suggest_reversibility("pay") == "irreversible"
    assert suggest_reversibility("wipe") == "irreversible"
    assert suggest_reversibility("refund") == "compensable"
    assert suggest_reversibility("get") is None


# --------------------------------------------------------------------------
# SQL DDL importer
# --------------------------------------------------------------------------
DDL = """
CREATE TABLE payments (
    id          bigserial PRIMARY KEY,
    amount      numeric(12,2) NOT NULL,
    currency    varchar(3),
    paid_at     timestamptz,
    approved    boolean,
    tenant_id   bigint NOT NULL,
    payee_id    bigint REFERENCES payees(id),
    CONSTRAINT amount_positive CHECK (amount > 0)
);

CREATE TABLE IF NOT EXISTS payees (
    id    serial PRIMARY KEY,
    name  text NOT NULL
);
"""


def test_sql_ddl_entities_and_types() -> None:
    draft = draft_from_sql(DDL, domain="payments")
    names = {e.name for e in draft.entities}
    assert names == {"Payment", "Payee"}
    payment = next(e for e in draft.entities if e.name == "Payment")
    props = {p.name: p for p in payment.properties}
    assert props["amount"].type == "decimal" and props["amount"].required
    assert props["currency"].type == "string" and not props["currency"].required
    assert props["paid_at"].type == "dateTime"
    assert props["approved"].type == "boolean"
    assert props["tenant_id"].type == "int"


def test_sql_ddl_skips_constraint_lines_and_hints_scope_keys() -> None:
    draft = draft_from_sql(DDL, domain="payments")
    payment = next(e for e in draft.entities if e.name == "Payment")
    names = {p.name for p in payment.properties}
    assert "amount_positive" not in names  # CHECK constraint is not a column
    tenant = next(p for p in payment.properties if p.name == "tenant_id")
    assert tenant.hint is not None and "scope" in tenant.hint.lower()
    payee = next(p for p in payment.properties if p.name == "payee_id")
    assert payee.hint is not None and "reference" in payee.hint.lower()


def test_sql_draft_emits_schema_valid_yaml() -> None:
    text = emit_yaml(draft_from_sql(DDL, domain="payments"))
    assert validate_registry_yaml(text) == []
    assert "TODO(review)" in text


# --------------------------------------------------------------------------
# OpenAPI importer
# --------------------------------------------------------------------------
OPENAPI: dict[str, Any] = {
    "openapi": "3.0.0",
    "info": {"title": "Ledger", "version": "1"},
    "paths": {
        "/payments": {
            "get": {"operationId": "listPayments"},
            "post": {
                "operationId": "payInvoice",
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["amount"],
                                "properties": {
                                    "amount": {"type": "number"},
                                    "currency": {"type": "string"},
                                    "urgent": {"type": "boolean"},
                                },
                            }
                        }
                    }
                },
            },
        },
        "/payments/{id}": {
            "get": {"operationId": "getPayment"},
            "delete": {"operationId": "deletePayment"},
        },
    },
}


def test_openapi_maps_methods_to_kinds() -> None:
    draft = draft_from_openapi(OPENAPI, domain="ledger")
    payment = next(e for e in draft.entities if e.name == "Payment")
    actions = {a.name: a for a in payment.actions}
    # GETs need no declared action — observe is implicit per entity (doc 06 §4)
    assert "listPayments" not in actions and "getPayment" not in actions
    assert actions["deletePayment"].kind == "record"
    assert actions["payInvoice"].kind == "effect"  # verb 'pay'
    data = {p.name: p for p in actions["payInvoice"].data}
    assert data["amount"].type == "decimal" and data["amount"].required
    assert data["urgent"].type == "boolean"


def test_openapi_draft_emits_schema_valid_yaml() -> None:
    text = emit_yaml(draft_from_openapi(OPENAPI, domain="ledger"))
    assert validate_registry_yaml(text) == []
    assert "TODO(review)" in text


# --------------------------------------------------------------------------
# MCP tool-list importer
# --------------------------------------------------------------------------
MCP_TOOLS: list[dict[str, Any]] = [
    {
        "name": "send_email",
        "description": "Send an email",
        "inputSchema": {
            "type": "object",
            "required": ["to"],
            "properties": {
                "to": {"type": "string"},
                "subject": {"type": "string"},
                "attachments": {"type": "integer"},
            },
        },
    },
    {"name": "get_customer", "inputSchema": {"properties": {"id": {"type": "string"}}}},
    {"name": "approve_order"},
    {"name": "frobnicate_widget"},
]


def test_mcp_tools_map_to_actions() -> None:
    draft = draft_from_mcp_tools(MCP_TOOLS, domain="crm")
    entities = {e.name: e for e in draft.entities}
    email = entities["Email"].actions[0]
    assert email.name == "send_email" and email.kind == "effect"
    assert email.suggested_reversibility == "irreversible"
    data = {p.name: p for p in email.data}
    assert data["to"].required and data["attachments"].type == "int"
    # observe tools need no declared action; the entity itself suffices
    assert "Customer" in entities and entities["Customer"].actions == []
    assert entities["Order"].actions[0].kind == "transition"
    # unknown verb: drafted as effect, flagged uncertain for the reviewer
    widget = entities["Widget"].actions[0]
    assert widget.kind == "effect" and widget.certain is False


def test_mcp_accepts_wrapped_tool_list() -> None:
    draft = draft_from_mcp_tools({"tools": MCP_TOOLS}, domain="crm")
    assert {e.name for e in draft.entities} == {"Email", "Customer", "Order", "Widget"}


def test_mcp_draft_emits_schema_valid_yaml() -> None:
    text = emit_yaml(draft_from_mcp_tools(MCP_TOOLS, domain="crm"))
    assert validate_registry_yaml(text) == []
    assert "TODO(review)" in text
    # uncertain kind guess is called out for the reviewer
    assert "kind guessed" in text


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def test_cli_end_to_end(tmp_path: Path) -> None:
    from stonefold_registry_gen.__main__ import main

    src = tmp_path / "tools.json"
    src.write_text(json.dumps(MCP_TOOLS), encoding="utf-8")
    out = tmp_path / "draft.registry.yaml"
    rc = main(["mcp", str(src), "--domain", "crm", "-o", str(out)])
    assert rc == 0
    text = out.read_text(encoding="utf-8")
    assert validate_registry_yaml(text) == []
    loaded = yaml.safe_load(text)
    assert loaded["domain"] == "crm"
    assert "Email" in loaded["entities"]
