# Domains & Use Cases — beyond the database

The most common misconception about this design is that it's "a safe way to let an AI use a database." It isn't. A database is **one connector**. The same five SIF kinds and the same Stele gates govern email, files, devices, cloud infrastructure, payments, external services — anything an agent can act on or about.

This companion shows that range. It is illustrative, not normative — the format is in [`00-RFC-sif-intent-format.md`](00-RFC-sif-intent-format.md), the policy language in [`01-RFC-agent-control-policy.md`](01-RFC-agent-control-policy.md).

## The one recipe (every domain follows it)

Adapting the gateway to any substrate is always the same four steps:

1. **Declare resources** in the registry (the things the agent acts on/about: a record, a file, a device, a channel, a service).
2. **Declare actions** on them, each with a **kind** (`observe / assess / record / effect / transition`) and governance **attributes** (reversibility, emission, operativeForce, resultSensitivity, explainability).
3. **Implement a connector** — the effect binding that actually does it (a SQL query, a file write, an SMTP send, a device command, an HTTP call).
4. **Write a policy** — `allow/deny/scope/gates` over those actions.

The agent emits the **same shape of intent** regardless of substrate, and **never knows which connector served it.** Adding a substrate is a new connector, not a new agent-facing surface.

---

## Domain vignettes

### 1. Business data — SQL (the baseline)
- **Resources/actions:** `Customer`, `Order` (`observe`); `Note` (`record`); `Invoice.send` (`transition`).
- **Connector:** SQL adapter → Postgres.
- **Intent:** `{ "kind":"observe", "entity":"Order", "filters":{ "status":"open" } }`
- **Key gate:** `scope: { Order: tenantOf(actor) }` → injected `WHERE tenant_id = …`.
- **Protects against:** cross-tenant reads, unscoped queries.

### 2. Email / messaging
- **Resources/actions:** `Email.send`, `Slack.post` (`effect`).
- **Connector:** SMTP / chat API.
- **Intent:** `{ "kind":"effect", "entity":"Email", "action":"send", "data":{ "to":"client@acme.com", "subject":"…", "body":"…" } }`
- **Key gates:** `allowlist` on recipient domain, `contentCheck: dlp.basic`, `rate: 20/hour`, approval for external sends.
- **Protects against:** data exfiltration via outbound mail (the classic injection payload), spam loops.

### 3. Files / documents
- **Resources/actions:** `File` (`observe` read, `record` write/delete), `Document.publish` / `share` (`effect`).
- **Connector:** filesystem / object store (S3) / DMS.
- **Intent:** `{ "kind":"effect", "entity":"Document", "action":"shareExternally", "filters":{ "id":"DOC-12" }, "data":{ "to":"partner@x.com" } }`
- **Key gates:** path/prefix `allowlist`, `disclosure` on file classification (block sharing `restricted` docs), size/`rate` caps, approval on external share.
- **Protects against:** reading outside permitted folders, leaking classified files, mass-delete.

### 4. Devices / IoT / cyber-physical
- **Resources/actions:** `Vehicle.applySpeed`, `Camera.capture`, `Valve.setPosition` (`effect`); `Sensor` (`observe`); device lifecycle (`transition`). Note `Camera.capture` and active radar have `emission: emits`.
- **Connector:** device driver / control bus.
- **Intent:** `{ "kind":"effect", "entity":"Vehicle", "action":"applySpeed", "data":{ "kph":30 } }`
- **Key gates:** `valueLimit` (0–130 kph), `precondition: [surroundingsClear, withinPostedLimit]`, `emissionControl` for emitting sensors.
- **Protects against:** unsafe physical commands, emitting (revealing position) without authorization. (See [`../examples/vehicle-controller.stele.yaml`](../spec/examples/vehicle-controller.stele.yaml).)

### 5. Cloud / DevOps infrastructure
- **Resources/actions:** `Service.restart` / `scale` (`effect`), `Resource.delete` (`effect`), `Deployment.promote` (`transition`), metrics (`observe`).
- **Connector:** cloud API / orchestrator (Kubernetes, Terraform).
- **Intent:** `{ "kind":"effect", "entity":"Service", "action":"restart", "filters":{ "name":"ingest-worker", "env":"prod" } }`
- **Key gates:** `window` (maintenance hours only), `denylist` on `env=prod` for destructive ops, `dualAuthorization` to delete prod resources, `requireApproval` to scale beyond N.
- **Protects against:** an agent (or an injected ticket/log) nuking production, out-of-window changes, runaway scale-ups.

### 6. Payments / finance
- **Resources/actions:** `Payment.pay` (`effect`), `LedgerEntry` (`record`), `Invoice.markPaid` (`transition`), accounts (`observe`).
- **Connector:** payment processor / banking API.
- **Intent:** `{ "kind":"effect", "entity":"Payment", "action":"pay", "data":{ "amount":800 }, "resolve":{ "payee":{ "entity":"Payee", "filters":{ "name":"Acme" } } } }`
- **Key gates:** tiered `requireApproval`/`dualAuthorization` by amount, `denylist` on sanctioned destinations, new-payee cooling-off `precondition`.
- **Protects against:** fraudulent wires (the runnable demo — [`05-demo-spec.md`](05-demo-spec.md), [`../examples/payments-ops.stele.yaml`](../spec/examples/payments-ops.stele.yaml)).

### 7. External services (data the system doesn't own)
- **Resources/actions:** `CreditScore`, `SanctionsListEntry`, `WeatherSnapshot`, `CriminalRecord` (`observe`, or `record` to capture a snapshot).
- **Connector:** REST / method-call adapter to a bureau, registry, or API (the LLM never knows it's not a local table).
- **Intent:** `{ "kind":"observe", "entity":"CreditScore", "filters":{ "person.nationalId":"…" } }`
- **Key gates:** `disclosure`/`resultSensitivity` (who may receive the result), `rate`/`quota` (paid APIs), scope by purpose-of-use.
- **Protects against:** over-querying paid/regulated sources, leaking results to the wrong sink.

### 8. Regulated records (healthcare, defence)
- **Resources/actions:** reads with sensitivity (`observe`), decisions like triage or combat-ID (`assess`), clinical/operational acts (`effect`), lifecycle steps (`transition`).
- **Connector:** EHR / mission systems.
- **Key gates:** `disclosure` + break-glass approval on sensitive reads, `requireExplanation` + human-confirm on `assess`, `quantityCap`/`precondition` on `effect`, `dualAuthorization` on the gravest actions.
- **Protects against:** privacy breaches, unaccountable decisions, unsafe or unauthorized actions. (See [`../examples/ward-nurse.stele.yaml`](../spec/examples/ward-nurse.stele.yaml), [`../examples/track-operator.stele.yaml`](../spec/examples/track-operator.stele.yaml).)

---

## Summary — same kinds, many substrates

| Domain | Substrate / connector | Example actions (kind) |
|---|---|---|
| Business data | SQL / Postgres | read records (`observe`), add note (`record`), send invoice (`transition`) |
| Email / chat | SMTP / chat API | send (`effect`) |
| Files / docs | filesystem / S3 / DMS | read (`observe`), write (`record`), share/publish (`effect`/`transition`) |
| Devices / IoT | device driver | set speed, capture, actuate (`effect`); read sensor (`observe`) |
| Cloud / DevOps | cloud API / k8s | restart, scale, delete (`effect`); promote (`transition`) |
| Payments | processor / bank API | pay (`effect`); mark paid (`transition`) |
| External services | REST / method-call | credit/sanctions/weather lookup (`observe`) |
| Regulated records | EHR / mission systems | sensitive read (`observe`), decision (`assess`), act (`effect`), lifecycle (`transition`) |

The invariant across all of them: **one intent shape, governed one way, audited one way — the agent proposes in domain vocabulary, and a deterministic layer disposes against whatever substrate happens to be behind the connector.** The database was never the point; it was the first connector.

*This document maps the technical substrates; who should deploy the gateway — the industries ranked by fit, their blocking risks, and the buyers — is [`13-who-is-this-for.md`](13-who-is-this-for.md).*
