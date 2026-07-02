# Track-S rung policies — S1 and S2 are author-owned

The benchmark's defense ladder (docs/15 §1) needs a policy per rung, over the **same**
payments registry the AP demo uses (`registry/acp-registry.yaml`), so only enforcement
strength varies:

| Rung | Policy | Who supplies it |
|---|---|---|
| **S0** naked tools | *(none — the gateway is bypassed)* | built in (`DirectBackend`) |
| **S1** allowlist only | `s1-allowlist.acp.yaml` | **the author** |
| **S2** allowlist + parameter policy | `s2-parameter.acp.yaml` | **the author** |
| **S3** SIF + ACP full | `examples/payments-ops.acp.yaml` | shipped |

## Why S1/S2 are not shipped

docs/15 §4.4 makes configuring the gateway baselines *in good faith* a **non-delegable
fairness call**: "S1/S2 use the strictest configuration the respective mechanism
supports for the scenario — beating a deliberately sloppy allowlist proves nothing."
The credibility of the S2→S3 gap (the entire argument, §1) depends on S1/S2 being the
strongest honest version of a commodity gateway — a judgement the author owns, not the
harness. So the harness ships S0 and S3 wired and reports S1/S2 as **UNCONFIGURED**
until their policy files exist here. It never fabricates a baseline.

## What each should contain

- **`s1-allowlist.acp.yaml`** — an allow-list of the payment/read *actions* by name,
  with **no** parameter gates, **no** scope predicates, **no** stateful counters: the
  commodity MCP-gateway posture (a call is permitted iff its tool name is allowed).
- **`s2-parameter.acp.yaml`** — S1 plus the best parameter-level controls a current
  gateway supports for this scenario: value bounds (`valueLimit`), recipient/country
  lists (`allowlist`/`denylist`). Still **no** scope injection, resolved-state reads,
  or stateful quota/spend counters — those are what S3 adds.

Author each as the strictest good-faith configuration of that mechanism, drop it in this
directory, and the harness picks it up automatically (`conditions.is_configured`).
