# SPDX-License-Identifier: Apache-2.0
"""The TCK fixture pack: one registry, a base policy, and per-check variants.

The registry is in the **authoring format** (docs/06, `schema/registry.schema.json`)
— the format the spec defines — so every implementation adapts from the same
artefact. The registered names below have REQUIRED semantics (docs/12 §3) the
driver must provide:

* scope predicate ``tckOwnedBy``    — row visible iff ``row.owner_id == actor.id``
* scope predicate ``tckTenantOf``   — row visible iff ``row.tenant == actor.claims["tenant"]``
* content hook    ``tck.rejectMarker`` — BLOCK iff the payload contains "BLOCK-ME"
* precondition    ``tck.flagSet``   — pass iff the resolved target's ``flag`` is true
* disclosure sink ``tckSink``       — the only sink a restricted read may flow to

The v0.6 names (drivers claiming ``hold-precondition`` / ``obligation``) read
the resolved TARGET's fields (like ``tck.flagSet``), so the world — not the
frozen payload — decides, and a resolved question stays resolved at the
dispatch-time re-validation:

* precondition ``tck.holdOnMarker`` — HOLD with code ``tck-queue`` iff the
  target's ``hold`` field is truthy; RAISE iff its ``crash`` field is truthy;
  else pass
* precondition ``tck.codelessHold`` — a CODE-LESS hold iff the target's
  ``badhold`` field is truthy; else pass (the gateway must resolve that hold
  FAIL — CS-026 rule 2)
* obligation registry ``tck.orders`` — a mock adapter (docs/12 §3): records
  seeded via ``seed_obligations``; ``reserve``/``consume``/``release``
  idempotent per (ref, intent id); reserving/consuming/releasing moves the
  record's ``line.state`` through ``reserved``/``consumed``/``unconsumed`` so
  an ``== 'unconsumed'`` match clause refuses a spoken-for line at decision
  time
"""

from __future__ import annotations

TCK_REGISTRY = """\
apiVersion: registry/v1.0
domain: tck

connectors:
  tck-data:    { type: sql }      # serves observe / record / transition
  tck-effects: { type: method }   # every effect binding
  tck-orders:  { type: method }   # the mock obligation-registry adapter (v0.6)

scopePredicates: [ tckOwnedBy, tckTenantOf ]
preconditionChecks:
  - tck.flagSet
  # v0.6 (CS-026/CS-029): hold-capable checks declare their codes + classes.
  - name: tck.holdOnMarker
    holdCapable: true
    reasonCodes:
      tck-queue: escalate
  - name: tck.codelessHold
    holdCapable: true
    reasonCodes:
      tck-never: terminal
hooks:              [ tck.rejectMarker ]
sinks:              [ tckSink ]

obligationRegistries:               # v0.6 (CS-034): the requireMatch source
  tck.orders:
    connector: tck-orders
    capability: transactional
    schema:
      vendorId: { type: string }
      state:    { values: [open, closed] }
      line:
        properties:
          amount: { type: decimal }
          state:  { values: [unconsumed, reserved, consumed] }

namedSets:
  tck-domains:           { values: [good.example, dual.example] }
  tck-blocked-domains:   { values: [dual.example, evil2.example] }
  tck-blocked-countries: { values: [XX] }

entities:

  Widget:
    dataSource: tck-data
    properties:
      id:       { type: string }
      owner_id: { type: string }     # scope key for tckOwnedBy
      name:     { type: string }

  Account:
    dataSource: tck-data
    properties:
      id:     { type: string }
      tenant: { type: string }       # scope key for tckTenantOf
      name:   { type: string }

  Order:
    dataSource: tck-data
    properties:
      id:           { type: string }
      currentState: { values: [pending, confirmed, cancelled] }
    actions:
      confirm: { kind: transition, from: [pending], to: confirmed }

  Payment:
    dataSource: tck-data
    properties:
      id:      { type: string }
      tenant:  { type: string }
      hold:    { type: boolean }   # read by tck.holdOnMarker (v0.6)
      crash:   { type: boolean }   # makes tck.holdOnMarker RAISE (v0.6)
      badhold: { type: boolean }   # read by tck.codelessHold (v0.6)
    actions:
      pay:
        kind: effect
        attributes: { reversibility: irreversible, operativeForce: high }
        connector: tck-effects
        data:
          amount:             { type: decimal, required: true }
          destinationCountry: { type: string }
          payeeId:            { type: string }
      zap:
        kind: effect
        attributes: { reversibility: irreversible }
        compensation: { resource: Payment, action: unzap }
        connector: tck-effects
      unzap:
        kind: effect
        connector: tck-effects

  Email:
    dataSource: tck-data
    properties:
      id: { type: string }
    actions:
      sendEmail:
        kind: effect
        attributes: { reversibility: irreversible }
        connector: tck-effects
        data:
          to:              { type: string }
          recipientDomain: { type: string }
          body:            { type: string }

  Sealed:
    dataSource: tck-data
    properties:
      id:     { type: string }
      secret: { type: string }
    actions:
      readSealed:
        kind: observe
        attributes: { resultSensitivity: restricted }

  Med:
    dataSource: tck-data
    properties:
      id:        { type: string }
      patientId: { type: string }
      flag:      { type: boolean }    # read by tck.flagSet
    actions:
      administer:
        kind: effect
        attributes: { reversibility: irreversible }
        connector: tck-effects
        data:
          drug:      { type: string }
          patientId: { type: string }
"""

# The base policy most checks run against — exercises every gate family.
TCK_POLICY = """\
apiVersion: stele/v0.1
agent: tck-agent
defaults: { failureMode: closed, audit: full }
killable: true

allow:
  - observe:    [Widget, Account, Order, Sealed, Med]
  - record:     [Widget]
  - effect:     [pay, sendEmail, administer, zap]
  - transition: { Order: [confirm] }

scope:
  Widget:  tckOwnedBy(actor)
  Account: tckTenantOf(actor)
  Payment: tckTenantOf(actor)

gates:
  pay:
    valueLimit: { field: data.amount, max: 10000 }
    denylist:   { field: data.destinationCountry, set: tck-blocked-countries }
    rate:       { limit: 2/hour, per: data.payeeId }
    requireApproval:
      when: "data.amount > 1000 and data.amount <= 5000"
      approvers: role:tck-approver
    dualAuthorization:
      when: "data.amount > 5000"
      approvers: role:tck-treasury
  sendEmail:
    allowlist:    { field: data.recipientDomain, set: tck-domains }
    contentCheck: tck.rejectMarker
  administer:
    precondition: [tck.flagSet]
    quantityCap:  { per: resource.patientId, limit: 2, window: 24h, of: data.drug }
  Order.confirm:
    precondition: { from: [pending] }
  readSealed:
    disclosure:
      when: "action.resultSensitivity == restricted"
      allowSink: [tckSink]
"""

# --- per-check policy variants -------------------------------------------
# A2: an explicit deny beats a matching allow.
POLICY_DENY_WINS = """\
apiVersion: stele/v0.1
agent: tck-agent
defaults: { failureMode: closed, audit: full }
allow:
  - effect: [pay]
deny:
  - effect: [pay]
"""

# A3: action-level and kind-level gates BOTH apply (AND) — stateless variant:
# "dual.example" passes the action-level allowlist but hits the kind-level
# denylist, so a deny proves the kind-level gate also ran.
POLICY_GATE_LAYERS = """\
apiVersion: stele/v0.1
agent: tck-agent
defaults: { failureMode: closed, audit: full }
allow:
  - effect: [sendEmail]
gates:
  sendEmail:
    allowlist: { field: data.recipientDomain, set: tck-domains }
  effect:
    denylist:  { field: data.recipientDomain, set: tck-blocked-domains }
"""

# C8: a condition referencing a path absent at runtime fails CLOSED.
POLICY_MISSING_PATH = """\
apiVersion: stele/v0.1
agent: tck-agent
defaults: { failureMode: closed, audit: full }
allow:
  - effect: [pay]
gates:
  pay:
    valueLimit: { field: data.amount, max: 100000, when: "resource.no_such_field == 1" }
"""

# C10 (v0.5 CS-024): disclosure.maxClassification compares by the DECLARED
# classification order; the ceiling here resolves from the session-supplied
# actor claim (the §7.12 ``actor.clearance`` form).
POLICY_CLASSIFICATION = """\
apiVersion: stele/v0.1
agent: tck-agent
defaults: { failureMode: closed, audit: full }
allow:
  - observe: [Widget, Sealed]
gates:
  readSealed:
    disclosure: { maxClassification: actor.clearance }
"""

# Lint fixtures — each MUST refuse to load (ERROR) or report (WARN).
POLICY_INVALID_OPEN_IRREVERSIBLE = """\
apiVersion: stele/v0.1
agent: tck-bad-agent
defaults: { failureMode: open, audit: basic }
allow:
  - effect: [pay]      # registry: irreversible
"""

POLICY_INVALID_UNKNOWN_NAME = """\
apiVersion: stele/v0.1
agent: tck-bad-agent
allow:
  - effect: [pay]
deny:
  - effect: [noSuchAction]
"""

POLICY_INVALID_STANDING_DENY = """\
apiVersion: stele/v0.1
agent: tck-bad-agent
allow:
  - observe: [Widget]
deny:
  - effect: [pay]
standing:
  - name: sometimes
    when: "context.mode == 'x'"
    enables: { effect: [pay] }
"""

POLICY_INVALID_DUAL_QUORUM = """\
apiVersion: stele/v0.1
agent: tck-bad-agent
allow:
  - effect: [pay]
gates:
  pay:
    dualAuthorization: { quorum: 1, approvers: role:tck-treasury }
"""

POLICY_WARN_STAR_GRANT = """\
apiVersion: stele/v0.1
agent: tck-warn-agent
defaults: { failureMode: closed, audit: full }
allow:
  - observe: '*'
"""

# A9 (§13 rule 18, CS-038): a check declared hold-capable with NO reasonCodes
# is a REGISTRY load error — every hold it returned would be code-less and
# resolve fail (CS-026 rule 2), so the declaration itself is refused. The
# minimal policy alongside is valid, isolating the refusal to the registry.
REGISTRY_INVALID_HOLD_NO_CODES = """\
apiVersion: registry/v1.0
domain: tck

connectors:
  tck-data: { type: sql }

preconditionChecks:
  - name: tck.badDecl
    holdCapable: true

entities:
  Widget:
    dataSource: tck-data
    properties:
      id: { type: string }
"""

POLICY_MINIMAL_OBSERVE = """\
apiVersion: stele/v0.1
agent: tck-agent
defaults: { failureMode: closed, audit: full }
allow:
  - observe: [Widget]
"""

# --- v0.6 variants ---------------------------------------------------------
# J1–J5 (CS-026/027/028): a hold-capable check gated with a resolver, composed
# with an approval tier above $1000 so J3 can prove BOTH contracts bind.
POLICY_HOLD = """\
apiVersion: stele/v0.1
agent: tck-agent
defaults: { failureMode: closed, audit: full }
killable: true
allow:
  - effect: [pay]
gates:
  pay:
    precondition:
      checks: [tck.holdOnMarker, tck.codelessHold]
      resolvers: role:tck-resolver
    requireApproval:
      when: "data.amount > 1000"
      approvers: role:tck-approver
"""

# J7 (CS-027): the same hold-capable check gated with NO resolvers. The TCK
# runs with NO deployment default resolver role (REQUIRED config, docs/12 §2),
# so a hold from this gate has no resolvable release contract and MUST be
# refused fail-closed (``hold-unresolvable``) — never staged. Loading this
# policy is legal (rule 18's second half is a WARN naming the fallback).
POLICY_HOLD_NO_RESOLVER = """\
apiVersion: stele/v0.1
agent: tck-agent
defaults: { failureMode: closed, audit: full }
killable: true
allow:
  - effect: [pay]
gates:
  pay:
    precondition:
      checks: [tck.holdOnMarker]
"""

# L1–L5 / M1–M4 / K2–K3 (CS-032–CS-036): the payment must correspond to
# exactly one open order line in the mock registry, within 10% tolerance;
# the line is reserved at staging and consumed at settlement.
POLICY_MATCH = """\
apiVersion: stele/v0.1
agent: tck-agent
defaults: { failureMode: closed, audit: full }
killable: true
allow:
  - effect: [pay]
gates:
  pay:
    requireMatch:
      registry: tck.orders
      match:
        - "obligation.vendorId == data.payeeId"
        - "obligation.state == 'open'"
        - "obligation.line.state == 'unconsumed'"
        - { field: obligation.line.amount, matches: data.amount, within: "10%" }
      consume: obligation.line
      onNoMatch: deny
      onAmbiguous: hold
      resolvers: role:tck-clerk
"""
