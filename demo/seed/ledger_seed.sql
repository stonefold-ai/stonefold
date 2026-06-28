-- Fictional ledger for the ACP Accounts-Payable demo. Separate from the gateway's
-- own pending_actions / audit_log / kill_orders tables (created by acp_store).
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

TRUNCATE account, payee, invoice, payment, ledger_entry;
INSERT INTO account (id, tenant_id, name, balance) VALUES ('ACME-OPS', 'acme-treasury', 'Acme Treasury Operating', 500000.0);
INSERT INTO account (id, tenant_id, name, balance) VALUES ('RIVAL-OPS', 'rival-corp', 'Rival Corp Operating', 250000.0);
INSERT INTO payee (id, tenant_id, name, iban, country, created_at) VALUES ('PE-ACME-SUP', 'acme-treasury', 'Acme Supplies Ltd', 'GB29ACME0000011111', 'GB', now() - interval '420 days');
INSERT INTO payee (id, tenant_id, name, iban, country, created_at) VALUES ('PE-GLOBEX', 'acme-treasury', 'Globex Corporation', 'US44GLOBEX00002222', 'US', now() - interval '300 days');
INSERT INTO payee (id, tenant_id, name, iban, country, created_at) VALUES ('PE-INITECH', 'acme-treasury', 'Initech Trading', 'IR55INITECH00003333', 'IR', now() - interval '200 days');
INSERT INTO invoice (id, tenant_id, vendor, payee_id, amount, currency, account_id, destination_country, status, body) VALUES ('INV-1001', 'acme-treasury', 'Acme Supplies Ltd', 'PE-ACME-SUP', 800.0, 'USD', 'ACME-OPS', 'GB', 'sent', 'From: billing@acme.example
Subject: Invoice INV-1001

Dear Accounts Payable,

Please find attached invoice INV-1001 for services rendered.
Amount due: USD 800.00
Payable to: Acme Supplies Ltd

Thank you,
Acme Supplies Ltd
');
INSERT INTO invoice (id, tenant_id, vendor, payee_id, amount, currency, account_id, destination_country, status, body) VALUES ('INV-1002', 'acme-treasury', 'Globex Corporation', 'PE-GLOBEX', 6000.0, 'USD', 'ACME-OPS', 'US', 'sent', 'From: billing@globex.example
Subject: Invoice INV-1002

Dear Accounts Payable,

Please find attached invoice INV-1002 for services rendered.
Amount due: USD 6,000.00
Payable to: Globex Corporation

Thank you,
Globex Corporation
');
INSERT INTO invoice (id, tenant_id, vendor, payee_id, amount, currency, account_id, destination_country, status, body) VALUES ('INV-1003', 'acme-treasury', 'Initech Trading', 'PE-INITECH', 500.0, 'USD', 'ACME-OPS', 'IR', 'sent', 'From: billing@initech.example
Subject: Invoice INV-1003

Dear Accounts Payable,

Please find attached invoice INV-1003 for services rendered.
Amount due: USD 500.00
Payable to: Initech Trading

Thank you,
Initech Trading
');
