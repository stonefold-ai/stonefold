# Policy used by the demo

The demo enforces the **unmodified** shipped policy — there is no demo-specific
rulebook (that is the point: the thing under test is the real product's policy).

The gateway loads, directly:

| File | What it is |
|---|---|
| [`../../examples/payments-ops.stele.yaml`](../../examples/payments-ops.stele.yaml) | the policy (allow/deny, scope, gates) |
| [`../../registry/stonefold-registry.yaml`](../../registry/stonefold-registry.yaml) | the model registry (resources, actions, scope predicates, named sets like `sanctioned-list`) |
| [`../../schema/stele.schema.json`](../../schema/stele.schema.json) | the JSON Schema the policy validates against |

These are a single source of truth (no copies here), so **editing the policy and
restarting the gateway changes the demo's behaviour with no code change** — e.g.:

- lower `gates.pay.valueLimit.max` and watch a previously-allowed payment refuse;
- change `requireApproval.when` thresholds and watch which amounts get held;
- add a country to the `sanctioned-list` named set in the registry and watch
  `denylist` block it.

After editing, `docker compose restart gateway` (or `make up`) to reload.
