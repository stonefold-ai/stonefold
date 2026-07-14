# Accounts-Payable Demo — the Stonefold Gateway in the path

A **real AI agent** doing accounts-payable work behind the **real Stonefold gateway**,
enforcing the **unmodified** [`spec/examples/payments-ops.stele.yaml`](../spec/examples/payments-ops.stele.yaml).
The bank and ledger are faked (no real money, all data fictional); the agent and
the enforcement are real.

The agent reads its invoice inbox and submits a payment intent for each invoice
through the gateway — **agent proposes, gateway disposes**. The gateway allows the
small payment, holds the mid-size one for human approval, and records every
decision. You see the raw prompt, the raw intents the model emitted, the gateway's
verdicts, and the audit log.

> **Note.** This is the *simple* demo. The original spec
> ([`docs/05-demo-spec.md`](../docs/05-demo-spec.md)) describes an adversarial
> version (an indirect-injection attack the gateway blocks); that part has been
> intentionally removed here for a clean, minimal walkthrough.

## What's here

```
demo/
├── docker-compose.yml      gateway + postgres + redis + agent
├── .env.example            ANTHROPIC_API_KEY=...  (copy to .env)
├── Makefile                up / seed / demo / run / down
├── gateway/Dockerfile      the Stonefold gateway service (FastAPI) + UI
├── agent/Dockerfile        the real-LLM agent runner
├── policy/                 pointer to the real policy (no copy — single source of truth)
├── seed/
│   ├── ledger_seed.sql     vendors, accounts, legitimate invoices
│   └── invoices/inbox/     acme_800.eml, globex_6000.eml
└── ui/                     raw transcript · approvals · live trace · kill switch (served by the gateway)
```

The Python lives in [`../src/stonefold_ap_demo/`](../src/stonefold_ap_demo) (so it is unit-
tested and `mypy --strict`-clean with the rest of the project); this directory is
the deployment + runbook.

## Run it (Docker + an API key)

```bash
git clone --recurse-submodules https://github.com/stonefold-ai/stonefold.git
                              # the spec/ submodule carries the policy + schemas the
                              # images copy in; if spec/ is empty: git submodule update --init
cd stonefold/demo
cp .env.example .env          # paste your ANTHROPIC_API_KEY (or set LLM_PROVIDER=openai / fake)

make up                       # build & start gateway + postgres + redis
make seed                     # load the fake ledger

# open the UI:
#   http://localhost:8088     live trace, raw transcript, approvals inbox, audit log

make run                      # run the agent against the live gateway (process the inbox)
make demo                     # OR a guided CLI walkthrough
make down                     # stop everything, remove volumes
```

`make` not installed? Use the `docker compose` equivalents (run from `demo/`):

```bash
docker compose --profile tools build   # the agent image is profile-gated; build it too
docker compose up -d
docker compose exec -T postgres psql -U stonefold -d stonefold < seed/ledger_seed.sql
docker compose run --rm agent python -m stonefold_ap_demo.agent_cli --scenario inbox
docker compose run --rm agent python -m stonefold_ap_demo            # walkthrough
docker compose down -v
```

No Docker and no key? The whole walkthrough still runs in-process with the fake LLM:

```bash
# from the repo root, in the project venv
python -m stonefold_ap_demo --provider fake
```

## The scenarios

| # | Scenario | What you see |
|---|---|---|
| **1** | **Happy path** — "Pay the approved invoice from Acme for $800." | known vendor, under cap → **paid**; trace shows intent → checks → effect |
| **2** | **Process the inbox** — "Process the new invoices in the inbox." | all three outcomes at once: the $800 is **allowed**, the $6,000 is **held** for approval, the sanctioned-country one is **denied** |
| **3** | **Approval** — "Pay the $6,000 invoice to Globex." | mid-size → **HELD**; appears in the approvals inbox; **Approve** → proceeds, **Reject** → doesn't (both outcomes are audited) |
| **4** | **Direct rejection** — "Pay the $500 invoice from Initech." | the vendor is in a sanctioned country → the **gateway refuses it itself** (`denylist`), no human involved |

The UI also has a **Gateway ON / OFF** toggle: ON, every payment is enforced
(allow / hold / deny); OFF, the agent's tools hit the bank directly and every
payment just executes — the contrast that shows what the gateway adds.

The top-right corner has a **KILL** button — the operator's emergency stop. One
click issues a global kill: from that moment every action at the gateway becomes
an audited **HALT** and no staged effect dispatches (invariant 5), so a re-run of
any scenario stops cold. The button toggles to **Lift kill** (and the page wears a
red frame while killed); lifting it lets the agent resume. Issuing and lifting are
themselves audited operator actions, and both show on the live trace. The kill is
a *gateway* control, so it only bites while the gateway is ON — with the gateway
OFF there is nothing in the path to halt.

In the UI, each run shows the **raw user + system prompt**, every **raw tool call**
(`read_inbox`, then a `submit_intent` per invoice with its exact `{resource, action, data}`),
and the gateway's raw decision for each — so nothing the agent sent is hidden.

## How a payment is decided (it's just the policy)

Each `pay` is checked by `payments-ops.stele.yaml`'s gates:
- `valueLimit` / `denylist` / `rate` apply to every payment;
- `requireApproval` holds a payment when `1000 < amount <= 10000` (so the $6,000 Globex
  invoice is held for a payments-manager), while the $800 Acme invoice passes;
- scope (`tenantOf`) means the agent can only pay from accounts in its own tenant.

Edit the policy (see [`policy/README.md`](policy/README.md)) and re-run to watch the
behaviour change with no code change.

## Why it's safe

All data is fictional — invented vendors, accounts, and IBANs; no real funds,
credentials, or PII. The "bank" is a Postgres table; a payment is a row plus an event.

## Notes / environment

- **Identity** comes from the transport (`X-Actor-Id` / `X-Session-Id` headers),
  never from the agent's request body (invariant 3). The gateway resolves the
  actor's tenant + roles from a server-side directory.
- **Postgres specifically** (not SQLite): the durable outbox uses real
  `SELECT … FOR UPDATE` for the staged-then-dispatched payment path.
- **API key** goes in `demo/.env` (docker-compose reads the `.env` next to the
  compose file). `LLM_PROVIDER=anthropic` (default), `openai`, or `fake`.
- **Behind an SSL-intercepting proxy?** The Dockerfiles set `PIP_TRUSTED_HOST` so
  the build survives it; if the agent's outbound HTTPS to the LLM is also
  intercepted, set `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` (your corporate CA) in `.env`.
- The gateway bind-mounts `./ui` and `../src`, so UI edits show on refresh and
  backend edits need only `docker compose restart gateway` (no rebuild).
