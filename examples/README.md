# Understanding a Policy — a worked example

This guide reads one policy end to end so you can read any of them, and — importantly — it marks **what the gateway provides for you** versus **what you must implement or register** to make a policy actually run.

We use [`ward-nurse.acp.yaml`](ward-nurse.acp.yaml). The same structure applies to every example here.

## Legend — where each referenced name comes from

When a policy mentions a name, it is one of these. The tags appear throughout the walkthrough.

- **[ENGINE]** — built into the gateway; you do **not** implement it per domain. The five action *kinds*, the four *decisions*, and all *gate types* (`disclosure`, `requireApproval`, `quantityCap`, `precondition`, `dualAuthorization`, `valueLimit`, `rate`, …) are engine-level.
- **[REGISTRY]** — declared in the **model registry** (the catalogue of your domain): entities, actions and their *kind*, lifecycle *states*, and *governance attributes* (`reversibility`, `operativeForce`, `resultSensitivity`, …). The policy reads these; it never sets them.
- **[REGISTER]** — a **named function or set you implement and register in the gateway**: scope predicates, precondition checks, content hooks, named sets, disclosure sinks. The policy refers to them by name.
- **[IDENTITY]** — supplied at runtime by your **identity provider / session**: the actor and their claims (e.g. `actor.ward`), the roles that approvers hold, and context flags like break-glass. Never supplied by the AI.
- **[AGENT DATA]** — values the agent puts in its request (`data.*`), validated against the registry.

---

## The walkthrough

### Header & defaults
```yaml
apiVersion: acp/v0.1
agent: ward-nurse-assistant
defaults: { failureMode: closed, audit: full }
killable: true
```
- `failureMode: closed` **[ENGINE]** — if any dependency errors, refuse rather than allow.
- `audit: full` **[ENGINE]** — record every attempt in full.
- `killable: true` **[ENGINE]** — an operator can halt this agent live.

### `allow` — the whitelist (anything not listed is denied)
```yaml
allow:
  - observe:    [Patient, Medication, Observation, Order]
  - assess:     [triage]
  - record:     [Observation]
  - effect:     [administer, pageOnCall]
  - transition: { Order: [sign], Encounter: [discharge] }
```
Every entity and action here must exist in the **[REGISTRY]** (`Patient`, `Medication`, `Observation`, `Order`, `Encounter`; the actions `triage`, `administer`, `pageOnCall`, `sign`, `discharge`, with their kinds). The *kinds* themselves (`observe`/`assess`/`record`/`effect`/`transition`) are **[ENGINE]**.

### `deny` — the hard "never" (always beats allow)
```yaml
deny:
  - effect:     [prescribe, discontinue]
  - transition: { Medication: [prescribe] }
```
`prescribe`, `discontinue`, `Medication.prescribe` must be declared in the **[REGISTRY]** so they can be named here (you deny things that exist).

### `scope` — which records, not just which actions
```yaml
scope:
  Patient:     inWard(actor.ward)
  Observation: forPatientInWard(actor.ward)
```
- `inWard`, `forPatientInWard` **[REGISTER]** — you implement these scope-predicate functions in the gateway; each returns a filter the connector applies below the model.
- `actor.ward` **[IDENTITY]** — the logged-in nurse's ward, from the session. The AI never sets it.

### `gates`

```yaml
observe:
  disclosure:
    when: "action.resultSensitivity == restricted"
    allowSink: [careTeam]
  requireApproval:
    when: "action.resultSensitivity == restricted and not exists context.breakGlass"
    approvers: role:charge-nurse
```
- `disclosure`, `requireApproval` **[ENGINE]** (gate types).
- `action.resultSensitivity` **[REGISTRY]** — the sensitivity attribute on the record being read. *Note:* this is often **row-dependent** (a specific patient's psych record is `restricted`), so the registry must supply it per-record/derived — which is why `disclosure` has a pre-check and post-check form (see RFC §7.12 / CS-002).
- `careTeam` **[REGISTER]** — a named disclosure *sink*; you define who/what counts as the care team.
- `context.breakGlass` **[IDENTITY]** — an emergency-access flag your session/context provides and the gateway verifies.
- `role:charge-nurse` **[IDENTITY]** — resolved to actual people by your identity system.

```yaml
triage:
  requireExplanation: true
  requireApproval: { when: "data.acuity <= 2", mode: confirm, approvers: role:clinician }
```
- `requireExplanation`, `requireApproval` **[ENGINE]**.
- `data.acuity` **[AGENT DATA]** — the score the agent produced (typed-checked against the registry).
- `role:clinician` **[IDENTITY]**.

```yaml
administer:
  precondition: [fiveRightsVerified, notDiscontinued]
  quantityCap:  { per: resource.patientId, limit: 3, window: 24h, of: data.drug }
  requireApproval: { when: "action.operativeForce == high", approvers: role:clinician }
```
- `precondition`, `quantityCap`, `requireApproval` **[ENGINE]** (gate types).
- `fiveRightsVerified`, `notDiscontinued` **[REGISTER]** — precondition-check functions you implement (deterministic; return pass/fail).
- `resource.patientId`, `data.drug` — the target's id **[REGISTRY]**-typed and the agent-supplied drug **[AGENT DATA]**.
- `action.operativeForce` **[REGISTRY]** — the action's operative-force attribute. *Note:* if "high-stakes" varies by drug, the registry must set/derive this per-administration (e.g. high-alert meds → `high`).

```yaml
Order.sign:
  precondition: { from: [draft] }
  requireApproval: { approvers: role:clinician }
```
- The `from: [draft]` check is **[ENGINE]**, but `Order`'s lifecycle (including the `draft` state) must be declared in the **[REGISTRY]**.
- `role:clinician` **[IDENTITY]**.

---

## What you must implement / register to run this policy

If you deployed `ward-nurse.acp.yaml`, this is your checklist. (The gateway and all gate types are assumed already built.)

**Declare in the model registry [REGISTRY]:**
- Entities: `Patient`, `Medication`, `Observation`, `Order`, `Encounter`.
- Actions + kinds: `observe` (Patient/Medication/Observation/Order), `assess: triage`, `record: Observation`, `effect: administer, pageOnCall, prescribe, discontinue`, `transition: Order.sign, Encounter.discharge, Medication.prescribe`.
- Lifecycle states: `Order` (incl. `draft`), `Encounter` (its dischargeable states).
- Attributes: `resultSensitivity` on readable records (often per-record/derived), `operativeForce` on `administer` (per drug), `reversibility: irreversible` on `administer`.

**Implement & register in the gateway [REGISTER]:**
- Scope predicates: `inWard`, `forPatientInWard`.
- Precondition checks: `fiveRightsVerified`, `notDiscontinued`.
- Disclosure sink: `careTeam`.

**Provide from identity / session [IDENTITY]:**
- `actor.ward` claim; the roles `charge-nurse` and `clinician` (and who holds them); the `context.breakGlass` flag.

**Provide connectors (effect bindings + data access):**
- `administer` → a medication-administration connector (eMAR/pump in production; a stub in a demo).
- `pageOnCall` → a paging connector.
- `observe` / `record` / `Order.sign` / `Encounter.discharge` → the data connector (EHR).

**Already provided by the gateway [ENGINE] — do not build per domain:**
- The five kinds, the four decisions, the pipeline, the outbox, kill, audit, and every gate type used above (`disclosure`, `requireApproval`, `quantityCap`, `precondition`, plus the rest of the catalog).

---

## How to read any other example

Same method: skim `allow`/`deny`/`scope`/`gates`, and for every name ask "which tag is this?" — **[ENGINE]** (free), **[REGISTRY]** (declare it), **[REGISTER]** (implement it), **[IDENTITY]** (from your IdP), or **[AGENT DATA]** (the request). The money demo's bindings for [`payments-ops.acp.yaml`](payments-ops.acp.yaml) are listed the same way in [`../docs/05-demo-spec.md`](../docs/05-demo-spec.md). The full meaning of every key is in the spec, [`../docs/01-RFC-agent-control-policy.md`](../docs/01-RFC-agent-control-policy.md).
