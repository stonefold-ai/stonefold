# stonefold_evidence — the audit evidence-pack exporter (plan G3)

Turns the gateway's audit log into a human-readable report (Markdown, PDF-ready) whose
sections are keyed to the controls they evidence, reusing the docs/14 EU AI Act mapping.
The compliance buyer forwards it to their auditor — the product's own output becomes
their deliverable.

```
# from a JSONL audit export (one AuditRecord per line):
python -m stonefold_evidence --jsonl audit.jsonl --policy examples/payments-ops.stele.yaml -o pack.md
# from a live Postgres audit_log table:
python -m stonefold_evidence --postgres "postgresql://stonefold@localhost/stonefold" -o pack.md
```

What the pack contains, one section per docs/14 row:

| Control | Evidence drawn from the log |
|---|---|
| **Art. 12 — Record-keeping** `[VERIFY]` | record count + decision breakdown; executed effects and their `resultRefs`; append-only/transactional property |
| **Art. 14 — Human oversight** `[VERIFY 14(4)]` | actions held for approval; approval contracts; actions halted by the kill-switch |
| **Art. 14 — oversight capacity** `[VERIFY 14(4)]` | every decision's deciding rule; per-gate results (`audit: full`) |
| **Art. 26 — Deployer obligations** `[VERIFY]` | the policy file as the documented control; log retention target |
| **DORA (adjacent)** `[VERIFY applicability]` | the same audit + approval evidence |

## Two hard rules

- **Read-only.** It only *reads* audit records (JSONL or Postgres) — no writes, no
  enforcement-path code.
- **`[VERIFY]` markers are printed verbatim.** The docs/14 citations are unverified
  until the author checks them against the regulation text; the pack never presents a
  control reference or date as confirmed, and states plainly that it is **not a
  compliance claim** (docs/14 honesty section travels in every pack).
