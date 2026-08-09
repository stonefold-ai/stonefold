# Renaming — from "Agent Control Policy" / `agent-control-protocol`

**July 2026, executed.** This project was previously called ACP; the repository was
`agent-control-protocol`. It is the same project. This page exists so anyone who
saw an old name can translate it, and so the change is not quietly hidden.

| Layer | Old name | Now |
|---|---|---|
| The product / gateway | ACP Gateway ("Agent Control Gateway") | **Stonefold** (the Stonefold Gateway) |
| The policy language (doc 01) | Agent Control Policy (ACP) | **Stele** (the Stonefold policy language) |
| The intent format (doc 00) | Structured Intent Format (SIF) | unchanged |
| Policy file `apiVersion` | `acp/v0.1` | `stele/v0.1` |
| Policy file extension | `*.acp.yaml` | `*.stele.yaml` |
| Policy JSON Schema | `schema/acp.schema.json` | `schema/stele.schema.json` |
| Python packages | `acp_*` | `stonefold_*` |
| Repository | `agent-control-protocol` | `stonefold` (old GitHub URLs redirect) |

The split is deliberate: **Stonefold** is the machine — the product, the gateway, the
code packages. **Stele** is the tablet it reads — the policy language and everything
written in it, so the file extension, the `apiVersion` and the schema carry that name.

## What the rename did not change

- **No normative semantics moved.** Identifiers, titles and file names only; no
  MUST/SHOULD/MAY wording changed. Version numbers were not bumped by the rename, and
  `stele/v0.1` accepts exactly the files `acp/v0.1` accepted.
- **The historical change sets** (`spec/docs/changeset-*.md`) were swept to the new
  identifiers as well, for the same reason: the project had no public users of the old
  names, so keeping stale identifiers in them served no reader. The version-to-version
  deltas themselves are unchanged and remain the way to bring an older implementation
  forward.
