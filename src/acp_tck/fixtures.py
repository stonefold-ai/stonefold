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
"""

from __future__ import annotations

TCK_REGISTRY = """\
apiVersion: registry/v1.0
domain: tck

connectors:
  tck-data:    { type: sql }      # serves observe / record / transition
  tck-effects: { type: method }   # every effect binding

scopePredicates:    [ tckOwnedBy, tckTenantOf ]
preconditionChecks: [ tck.flagSet ]
hooks:              [ tck.rejectMarker ]
sinks:              [ tckSink ]

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
      id:     { type: string }
      tenant: { type: string }
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
apiVersion: acp/v0.1
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
apiVersion: acp/v0.1
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
apiVersion: acp/v0.1
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
apiVersion: acp/v0.1
agent: tck-agent
defaults: { failureMode: closed, audit: full }
allow:
  - effect: [pay]
gates:
  pay:
    valueLimit: { field: data.amount, max: 100000, when: "resource.no_such_field == 1" }
"""

# Lint fixtures — each MUST refuse to load (ERROR) or report (WARN).
POLICY_INVALID_OPEN_IRREVERSIBLE = """\
apiVersion: acp/v0.1
agent: tck-bad-agent
defaults: { failureMode: open, audit: basic }
allow:
  - effect: [pay]      # registry: irreversible
"""

POLICY_INVALID_UNKNOWN_NAME = """\
apiVersion: acp/v0.1
agent: tck-bad-agent
allow:
  - effect: [pay]
deny:
  - effect: [noSuchAction]
"""

POLICY_INVALID_STANDING_DENY = """\
apiVersion: acp/v0.1
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
apiVersion: acp/v0.1
agent: tck-bad-agent
allow:
  - effect: [pay]
gates:
  pay:
    dualAuthorization: { quorum: 1, approvers: role:tck-treasury }
"""

POLICY_WARN_STAR_GRANT = """\
apiVersion: acp/v0.1
agent: tck-warn-agent
defaults: { failureMode: closed, audit: full }
allow:
  - observe: '*'
"""
