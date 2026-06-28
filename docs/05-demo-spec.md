# Demo Specification — Accounts-Payable Assistant

A runnable, check-out-and-run demonstration that uses a **real LLM agent** (an API key is required) and shows the gateway **enforcing policy on every action a real agent takes** — allowing a routine payment, holding a mid-size one for a human, and refusing a non-compliant one — with a one-click **gateway ON/OFF** toggle that shows what happens with no gateway in the path. The shipped demo lives in [`demo/`](../demo/).

> **Note.** This is the *simplified* demo: a clean enforcement walkthrough. An earlier version of this spec described an adversarial indirect-injection scenario (a malicious invoice, a kill switch, a spend-cap loop, an "invite-attack" box); that was removed in favour of the minimal flow below. The underlying gateway still supports kill, scope, and every gate — they are simply not all surfaced in this demo's UI.

## Goal

A viewer clones the repo, supplies an API key, runs one command, and within ~5 minutes sees: a real AI agent processing an invoice inbox; the gateway **allowing** a routine payment, **holding** a mid-size one for human approval, and **refusing** one to a sanctioned-country vendor — each decision shown live (tagged with the pipeline stage that produced it) and recorded. Flipping the gateway **off** shows the same agent's payments executing with no checks at all — the contrast that motivates the product.

## Domain and why

An **AI Accounts-Payable assistant** that reads invoices and pays vendors — the textbook high-risk case: the agent handles private financial data, ingests vendor invoices, and can move money. The bank and ledger are **faked** (no real money, no PII, all data fictional); the agent and the gateway enforcement are real.

The demo's rulebook is the **unmodified** policy [`examples/payments-ops.acp.yaml`](../examples/payments-ops.acp.yaml) — there is no demo-specific policy. Editing that file and re-running changes behaviour with no code change.

## Components / infrastructure

Everything comes up via `docker compose` from a clean checkout, given an API key.

| Component | What it is | Notes |
|---|---|---|
| **ACP Gateway** | the real product (FastAPI) + the demo UI | the agent reaches it only through the SIF-native `submit_intent` tool |
| **Agent runner** | a tool-use loop calling a **real LLM** (Claude default; OpenAI supported) | needs `ANTHROPIC_API_KEY`; cheap model (e.g. Haiku). The UI also runs it in-process; a scripted **fake-LLM** mode needs no key. |
| **Postgres** | the fake **ledger** (accounts, payees, invoices, payments) **and** the gateway's `audit_log`, `pending_actions` (outbox), `kill_orders` | Postgres specifically — the durable outbox uses real `SELECT … FOR UPDATE` |
| **Redis** | rate counters | |
| **Fake connector** | `ledger-pay` — "sends" money by writing a payment row + emitting an event | clearly fake and safe |
| **Demo UI** | a thin web page: a **gateway ON/OFF toggle**, scenario buttons, the **raw agent transcript** (prompt + the exact intents the model emits), a **live trace** (intent → decision → effect, each tagged by pipeline stage), and an **approvals inbox** (Approve/Reject) | REST + WebSocket |
| **Seed data** | accounts, known payees, and three legitimate invoices | one routine, one mid-size (approval), one to a sanctioned-country vendor (refused) |

A **fake-LLM mode** exists (scripted decisions) so CI and no-key users can run the mechanics — the real demo uses a key.

## Domain bindings the demo implements

The policy only *names* the domain-specific functions and data it relies on; the demo implements them, or the policy will not load (the linter requires every referenced name to exist). The generic **gate types** (`denylist`, `valueLimit`, `rate`, `requireApproval`, `dualAuthorization`, `precondition`) are part of the gateway engine and are **not** re-implemented here.

| Binding | Type | What the demo implements |
|---|---|---|
| `tenantOf(actor)` | scope predicate | limits `Account`/`Payment` rows to the actor's tenant, and gates paying *from* an out-of-tenant account (a pre-resolution check) |
| `sanctioned-list` | named set | the country list the `denylist` gate checks against `data.destinationCountry` — trips on the Initech (IR) invoice |
| `payeeCoolingOffElapsed` | precondition check | the new-payee hold (runs `when: exists data.newPayee`); applies to any newly-introduced payee — not exercised by the shipped inbox, which uses known payees |
| registry: entities/actions | registry | `Account`/`Payment`/`Payee` (observe), `LedgerEntry` (record), `pay` (effect, staged via the outbox), `Invoice` |
| `ledger-pay` | connector | the fake "bank": carries out `pay` by writing a payment row + emitting an event; reads apply the injected scope below the model |
| `role:payments-manager` | identity/role | the approver for held mid-size payments (Approve/Reject in the UI) |
| seed data | fixtures | the `ACME-OPS` account; known payees; invoices `acme_800` (routine), `globex_6000` (approval), `initech_500` (sanctioned country → refused) |

## Repo layout

```
demo/
├── docker-compose.yml         # gateway + postgres + redis + agent
├── .env.example               # ANTHROPIC_API_KEY=...  (copy to .env)
├── Makefile                   # up / seed / demo / run / down
├── policy/                    # pointer to examples/payments-ops.acp.yaml (single source of truth)
├── seed/
│   ├── ledger_seed.sql        # accounts, payees, invoices
│   └── invoices/inbox/
│       ├── acme_800.eml       # routine     — allowed
│       ├── globex_6000.eml    # mid-size    — held for approval
│       └── initech_500.eml    # sanctioned  — refused (denylist)
├── gateway/Dockerfile         # the ACP gateway service + UI
├── agent/Dockerfile           # the real-LLM agent runner
├── ui/                        # transcript · live trace · approvals
└── README.md                  # the runbook (below)
```

The Python lives in [`../src/acp_ap_demo/`](../src/acp_ap_demo) so it is unit-tested and `mypy --strict`-clean with the rest of the project; `demo/` is the deployment + runbook.

## The scenarios

Each is a real prompt to the real LLM; the agent decides; the gateway enforces. The UI's **gateway toggle** decides whether the gateway is in the path.

1. **Happy path** — *"Pay the approved invoice from Acme for $800."* Known vendor, under cap → **allowed** and paid. Trace shows intent → checks → effect.
2. **Process the inbox** — *"Process the new invoices in the inbox."* The agent submits a payment intent per invoice; the gateway returns all three outcomes at once: **$800 allowed**, **$6,000 held** for approval, **$500 (sanctioned country) denied** on `denylist`.
3. **Approval** — *"Pay the $6,000 invoice to Globex."* Mid-size → **HELD**; appears in the approvals inbox; **Approve** → it proceeds (money moves), **Reject** → it does not. Both outcomes are audited.
4. **Direct rejection** — *"Pay the $500 invoice from Initech."* The vendor is in a sanctioned country → the gateway **refuses it itself** (`denylist`), with no human involved.
5. **Gateway off (the contrast)** — flip the toggle to **OFF** and re-run any scenario: the agent's tools hit the ledger directly, every payment executes with **no** checks (the $6,000 is not held, the $500 is not refused), and nothing is recorded — showing exactly what the gateway adds.

Every decision (allow / hold / deny) is an append-only audit record with its reason; the live trace tags each entry with the pipeline stage that produced it (`RESOLVE` / `AUTHORIZE` / `SCOPE` / `GATES` / `EXECUTE` / `DISPATCH`).

## Setup (the README runbook)

```bash
# Prereqs: Docker, and an API key (Anthropic by default; OpenAI supported).
git clone <repo> && cd <repo>/demo
cp .env.example .env          # paste your ANTHROPIC_API_KEY

make up                       # docker compose up --build (gateway, postgres, redis)
make seed                     # load the fake ledger (accounts, payees, invoices)

open http://localhost:8088    # UI: toggle, scenario buttons, transcript, live trace, approvals
make run                      # OR run the agent against the live gateway from the CLI
make demo                     # OR a guided CLI walkthrough

make down
```

## Definition of done

- `make up && make seed` brings the whole demo up from a clean checkout with only Docker + an API key.
- Through the gateway, the inbox run shows **allow / hold / deny**; the held payment can be **approved or rejected**; with the toggle **off**, the same payments execute directly — the side-by-side contrast.
- Automated scenario tests (`tests/test_ap_demo_*.py`) cover happy / process-inbox / approval / reject / direct-rejection / gateway-off in fake-LLM mode (no key, no Docker), plus a Postgres + Redis integration test.
- The demo uses the unmodified [`examples/payments-ops.acp.yaml`](../examples/payments-ops.acp.yaml); editing that file and re-running changes behaviour with no code change.
- All data is fictional; no real funds, credentials, or PII anywhere.
