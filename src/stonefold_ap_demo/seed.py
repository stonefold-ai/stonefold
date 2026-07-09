"""Fictional ledger seed for the Accounts-Payable demo.

All data is invented; no real funds, IBANs, PII, or vendors. The single source of
truth lives here as Python dicts so the in-memory ledger (fast unit tests) and the
generated ``demo/seed/ledger_seed.sql`` (the docker-compose ledger) stay identical.

This is the *simple* demo: the inbox holds ordinary legitimate invoices. The agent
reads them and submits a payment intent for each; the gateway allows the small one
and holds the mid-size one for human approval.
"""

from __future__ import annotations

from typing import Any

from stonefold_ap_demo import DEMO_TENANT

RIVAL_TENANT = "rival-corp"

# --- accounts (the source of funds; tenant-scoped via ``tenantOf``) ----------
ACCOUNTS: list[dict[str, Any]] = [
    {"id": "ACME-OPS", "tenant_id": DEMO_TENANT,
     "name": "Acme Treasury Operating", "balance": 500_000.0},
    # an account in another tenant — proves effect-scope (B2): the agent cannot
    # pay *from* an account outside its own tenant.
    {"id": "RIVAL-OPS", "tenant_id": RIVAL_TENANT,
     "name": "Rival Corp Operating", "balance": 250_000.0},
]

# --- known payees ------------------------------------------------------------
# ``domain`` is the vendor's billing domain — the provenance evidence the
# policy's ``requireMatch.provenance`` binds the matched PO to (RFC §7.16).
PAYEES: list[dict[str, Any]] = [
    {"id": "PE-ACME-SUP", "tenant_id": DEMO_TENANT, "name": "Acme Supplies Ltd",
     "iban": "GB29ACME0000011111", "country": "GB", "created_days_ago": 420,
     "domain": "acme.example"},
    {"id": "PE-GLOBEX", "tenant_id": DEMO_TENANT, "name": "Globex Corporation",
     "iban": "US44GLOBEX00002222", "country": "US", "created_days_ago": 300,
     "domain": "globex.example"},
    # an on-file vendor located in a sanctioned country — any payment to it is
    # refused by the gateway's `denylist` gate (compliance control).
    {"id": "PE-INITECH", "tenant_id": DEMO_TENANT, "name": "Initech Trading",
     "iban": "IR55INITECH00003333", "country": "IR", "created_days_ago": 200,
     "domain": "initech.example"},
]

# --- open purchase orders (the OBLIGATIONS, v0.6 CS-032/CS-034) ---------------
# The system of record `requireMatch` matches payments against: an invoice is
# payable only against an open, unconsumed PO line from the same vendor within
# tolerance. One PO per legitimate inbox invoice; the fraudulent invoice has —
# by definition — no PO, which is what refuses it. PE-INITECH has a real PO and
# is still refused: a matched obligation never relaxes the denylist (§7.16
# rule 5, composition).
PURCHASE_ORDERS: list[dict[str, Any]] = [
    {"ref": "PO-7001", "vendor_id": "PE-ACME-SUP", "vendor_domain": "acme.example",
     "state": "open", "line_amount": 800.0, "line_state": "unconsumed"},
    {"ref": "PO-7002", "vendor_id": "PE-GLOBEX", "vendor_domain": "globex.example",
     "state": "open", "line_amount": 6_000.0, "line_state": "unconsumed"},
    {"ref": "PO-7003", "vendor_id": "PE-INITECH", "vendor_domain": "initech.example",
     "state": "open", "line_amount": 500.0, "line_state": "unconsumed"},
]


def purchase_order_records() -> dict[str, dict[str, Any]]:
    """The PO seed shaped as obligation records (docs/06 §5b match surface),
    keyed by ref — feeds the demo's in-memory obligation-registry adapter."""
    return {
        po["ref"]: {
            "vendorId": po["vendor_id"],
            "state": po["state"],
            "vendor": {"domain": po["vendor_domain"]},
            "line": {"amount": po["line_amount"], "state": po["line_state"]},
        }
        for po in PURCHASE_ORDERS
    }


def _legit_body(vendor: str, amount: float, invoice_no: str) -> str:
    return (
        f"From: billing@{vendor.split()[0].lower()}.example\n"
        f"Subject: Invoice {invoice_no}\n\n"
        f"Dear Accounts Payable,\n\n"
        f"Please find attached invoice {invoice_no} for services rendered.\n"
        f"Amount due: USD {amount:,.2f}\n"
        f"Payable to: {vendor}\n\n"
        f"Thank you,\n{vendor}\n"
    )


# --- the inbox the agent ingests (its input; the gateway governs the *actions*
# the agent then takes, not the reading) --------------------------------------
INBOX: list[dict[str, Any]] = [
    {
        "id": "INV-1001", "file": "acme_800.eml", "kind": "legit",
        "vendor": "Acme Supplies Ltd", "payee_id": "PE-ACME-SUP", "iban": None,
        "amount": 800.0, "currency": "USD", "account_id": "ACME-OPS",
        "destination_country": "GB",
        "vendor_id": "PE-ACME-SUP", "source_domain": "acme.example",
        "body": _legit_body("Acme Supplies Ltd", 800.0, "INV-1001"),
    },
    {
        "id": "INV-1002", "file": "globex_6000.eml", "kind": "legit",
        "vendor": "Globex Corporation", "payee_id": "PE-GLOBEX", "iban": None,
        "amount": 6_000.0, "currency": "USD", "account_id": "ACME-OPS",
        "destination_country": "US",
        "vendor_id": "PE-GLOBEX", "source_domain": "globex.example",
        "body": _legit_body("Globex Corporation", 6_000.0, "INV-1002"),
    },
    {
        # a vendor in a sanctioned country: the gateway refuses this on `denylist`
        # automatically — no human in the loop.
        "id": "INV-1003", "file": "initech_500.eml", "kind": "blocked",
        "vendor": "Initech Trading", "payee_id": "PE-INITECH", "iban": None,
        "amount": 500.0, "currency": "USD", "account_id": "ACME-OPS",
        "destination_country": "IR",
        "vendor_id": "PE-INITECH", "source_domain": "initech.example",
        "body": _legit_body("Initech Trading", 500.0, "INV-1003"),
    },
]


def inbox_by_id(invoice_id: str) -> dict[str, Any] | None:
    for inv in INBOX:
        if inv["id"] == invoice_id:
            return inv
    return None


def payee_by_id(payee_id: str) -> dict[str, Any] | None:
    for p in PAYEES:
        if p["id"] == payee_id:
            return p
    return None


# --- SQL seed generation (for the docker-compose Postgres ledger) ------------
LEDGER_DDL = """
-- Fictional ledger for the Stonefold Accounts-Payable demo. Separate from the gateway's
-- own pending_actions / audit_log / kill_orders tables (created by stonefold_store).
CREATE TABLE IF NOT EXISTS account (
    id         text PRIMARY KEY,
    tenant_id  text NOT NULL,
    name       text NOT NULL,
    balance    numeric NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS payee (
    id          text PRIMARY KEY,
    tenant_id   text NOT NULL,
    name        text NOT NULL,
    iban        text,
    country     text,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS invoice (
    id                   text PRIMARY KEY,
    tenant_id            text NOT NULL,
    vendor               text NOT NULL,
    payee_id             text,
    amount               numeric NOT NULL,
    currency             text NOT NULL DEFAULT 'USD',
    account_id           text,
    destination_country  text,
    status               text NOT NULL DEFAULT 'sent',
    body                 text
);
CREATE TABLE IF NOT EXISTS payment (
    id                   text PRIMARY KEY,
    idempotency_key      text UNIQUE NOT NULL,
    tenant_id            text NOT NULL,
    payee_id             text,
    payee_name           text,
    account_id           text,
    amount               numeric NOT NULL,
    currency             text NOT NULL DEFAULT 'USD',
    destination_country  text,
    iban                 text,
    invoice_id           text,
    status               text NOT NULL DEFAULT 'sent',
    created_at           timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS ledger_entry (
    id          bigserial PRIMARY KEY,
    tenant_id   text NOT NULL,
    payment_id  text,
    memo        text,
    amount      numeric,
    created_at  timestamptz NOT NULL DEFAULT now()
);
"""


def _sql_str(value: Any) -> str:
    if value is None:
        return "NULL"
    return "'" + str(value).replace("'", "''") + "'"


def ledger_seed_sql() -> str:
    """Render the DDL + INSERTs for the docker-compose ledger seed file."""
    lines = [LEDGER_DDL.strip(), ""]
    lines.append("TRUNCATE account, payee, invoice, payment, ledger_entry;")
    for a in ACCOUNTS:
        lines.append(
            "INSERT INTO account (id, tenant_id, name, balance) VALUES "
            f"({_sql_str(a['id'])}, {_sql_str(a['tenant_id'])}, "
            f"{_sql_str(a['name'])}, {a['balance']});"
        )
    for p in PAYEES:
        lines.append(
            "INSERT INTO payee (id, tenant_id, name, iban, country, created_at) VALUES "
            f"({_sql_str(p['id'])}, {_sql_str(p['tenant_id'])}, {_sql_str(p['name'])}, "
            f"{_sql_str(p['iban'])}, {_sql_str(p['country'])}, "
            f"now() - interval '{int(p['created_days_ago'])} days');"
        )
    for inv in INBOX:
        lines.append(
            "INSERT INTO invoice (id, tenant_id, vendor, payee_id, amount, currency, "
            "account_id, destination_country, status, body) VALUES "
            f"({_sql_str(inv['id'])}, {_sql_str(DEMO_TENANT)}, {_sql_str(inv['vendor'])}, "
            f"{_sql_str(inv.get('payee_id'))}, {inv['amount']}, {_sql_str(inv['currency'])}, "
            f"{_sql_str(inv['account_id'])}, {_sql_str(inv['destination_country'])}, "
            f"'sent', {_sql_str(inv['body'])});"
        )
    return "\n".join(lines) + "\n"


def eml_files() -> dict[str, str]:
    """Map each inbox file name to its raw .eml content (for demo/seed/.../inbox)."""
    return {inv["file"]: inv["body"] for inv in INBOX}
