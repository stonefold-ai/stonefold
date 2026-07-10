# 05 — Obligation matching (v0.6): in bounds is not the same as owed

**The question this example answers:** every gate so far bounds damage. None
can catch the payment that is under every limit and corresponds to
**nothing** — no order was ever placed, or the invoice is already paid. Who
builds what to close that?

| File | Role | What it is |
|---|---|---|
| `registry.yaml` | platform / policy team | now DECLARES the obligation registry: which system of record, which typed fields (a match surface, not a domain model) |
| `policy.stele.yaml` | policy author | the `requireMatch` rule — *"a payment must match an open purchase order, within 10%, and spends it"* — in the reviewed file, not in code |
| **`erp_adapter.py`** | **function developer** | the four-operation door to YOUR system of record: `query / reserve / consume / release` |
| `gateway_service.py` | infra engineer | hands ONE adapter instance to the three consumers of the obligation's lifecycle |
| **`agent.py`** | **agent developer** | the convergence loop: `retryClass` tells your program fix-and-resubmit from give-up |
| `main.py` | demo driver | runs the loop over the wire; reads the consumption receipt from the audit API |

---

## Step 1 (platform team) — declare the registry in `registry.yaml`

```yaml
obligationRegistries:
  erp.purchase_orders:
    connector: erp-adapter
    capability: transactional
    schema:
      vendorId: { type: string }
      state:    { values: [open, closed] }
      line:
        properties:
          amount: { type: decimal }
          state:  { values: [unconsumed, reserved, consumed] }
```

Declare **only the fields policies compare and consume** — a purchase order
has a hundred fields; the match reads five. Free text is never a match
input. One deployment rule to say loudly: **the agent's principal must not
have write access to this system** — an agent that can create orders and
then pay against them approves itself (the linter errors where that overlap
is statically visible).

## Step 2 (policy author) — the rule in `policy.stele.yaml`

```yaml
requireMatch:
  registry: erp.purchase_orders
  match:
    - "obligation.vendorId == data.vendorId"
    - "obligation.state == 'open'"
    - "obligation.line.state == 'unconsumed'"
    - { field: obligation.line.amount, matches: data.amount, within: "10%" }
  consume: obligation.line
  onNoMatch: deny          # or hold -> a human queue
  onAmbiguous: hold        # several matches: NEVER auto-pick
  resolvers: role:ap-clerk
```

Read the semantics off the page: exactly **one** open record within
tolerance passes; **zero** resolves `onNoMatch`; **several** hold for the
named human — the gateway never picks. Every `obligation.*` value is read
from the registry's response, never from the agent (a forged copy in `data`
changes nothing). And crucial: a matched obligation **never relaxes** any
other gate — limits and approvals still apply on top.

*Simpler start:* a plain precondition check (example 03) that queries your
ERP gives the same protection with zero declarations. Reach for
`requireMatch` when you want the rule in the reviewed file, ambiguity routed
to a named resolver, or gateway-managed spending because your record system
doesn't mark things spent.

## Step 3 (function developer) — `erp_adapter.py`

Four idempotent operations against your system of record:

```
query(selector)          -> matching typed records
reserve(ref, intent_id)  -> claim a record for one staged action
consume(ref, intent_id)  -> mark it spent, return a receipt
release(ref, intent_id)  -> give it back (cancellation, expiry)
```

Idempotent **per (ref, intent_id)**: a retry never double-consumes, and
releasing something already expired is a no-op. Reservations carry a TTL on
*your system's* clock, so a crashed gateway can never lock a real order line
forever. The guide uses the shipped in-memory reference; production
implements the same four calls against the real ERP.

## Step 4 (infra engineer) — one adapter, three consumers

```python
adapters = {"erp.purchase_orders": erp_adapter.build_adapter()}
engine  = DefaultGateEngine(registry, obligations=adapters)   # decision-time query
gateway = Gateway(..., obligations=adapters, dedupe_window_s=3600.0)  # reserve @ staging
worker  = DispatchWorker(..., obligations=adapters)           # consume @ settle, release @ cancel
```

That lifecycle is the double-spend answer: the matched line is **reserved
inside the staging commit** (a second intent can't claim it in the
decide→dispatch gap), **consumed with the settle** (receipt in the audit),
**released on any cancellation** (the line frees for a resubmit). Duplicate
holds within the dedupe window collapse into one queue item with an attempt
count — holds spend human attention.

## Step 5 (agent developer) — the loop that converges

Your agent reads two fields off every refusal:

```
deny  code=outside-tolerance class=retryable   -> fix the intent, resubmit
deny  code=no-match          class=terminal    -> stop; nothing to fix
deny  code=...               class=escalate    -> stop; surface to YOUR human
hold                                            -> a gateway human owns it; wait
```

`agent.py`'s `converge()` is the whole pattern in ten lines. Note what the
agent *never* sees at the default visibility: the record-side values it was
compared against — the channel converges without becoming an oracle.

## Run it

```bash
python guide/05_obligation_matching/main.py
```

Expected output:

```
agent[s1]: pay ACME      990.0 -> deny  code=outside-tolerance class=retryable
agent[s1]: retryable -> re-extracting the amount...
agent[s1]: pay ACME      800.0 -> allow
driver: line consumed, receipt rcpt_...
agent[s1]: pay ACME      800.0 -> deny  code=no-match class=terminal
agent[s1]: pay QUICKPAY 4500.0 -> deny  code=no-match class=terminal
driver: one line, one payment, ever
```

## What to notice

1. **The second `pay 800` is the v0.6 moment.** Same vendor, same amount,
   under every limit — refused, because the order line is *spent*. No
   constant-comparing gate can express that; a relation to an external
   record can.
2. **The refusal classes drove the loop.** `retryable` → the agent fixed its
   extraction and converged; `terminal` → it stopped. No prose parsing, no
   flailing. (The guide's `converge()` scripts the corrected amount; the same
   loop with a live LLM actually re-extracting the invoice is the
   Accounts-Payable demo — `demo/` + `docs/05-demo-spec.md`.)
3. **The receipt is evidence.** The audit record for the settle carries
   `obligationRefs` (what entitled the payment) and `consumption` (the spend
   receipt) — reconciliation has both ends of the relation.
