"""The TCK driver contract — the ONE interface an implementation adapts.

The TCK never imports a gateway; it talks to a ``ConformanceDriver`` the
implementer writes (docs/12). The driver is a thin, test-only adapter over the
implementation under test: it loads a registry+policy, seeds world state,
submits intents *as an authenticated actor* (the TCK plays the transport, so
actor/session arrive the way invariant 3 requires — never in the payload),
steps the dispatch worker deterministically, and exposes what happened
(effects that left, audit records written).

Determinism is a driver obligation: ``set_clock`` must control every
time-based decision (the RFC already mandates an injected clock), and
``dispatch_once`` must run the staged-effect worker synchronously to
completion. A driver that cannot satisfy an obligation omits the matching
capability and the dependent checks are SKIPPED — never silently passed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

# --- capabilities (a driver advertises what it can do; checks require them) ---
CAP_LINT_WARNINGS = "lint-warnings"  # LoadResult carries WARN-level findings
CAP_STAGING = "staging"  # effects are staged; dispatch_once() steps the worker
CAP_APPROVALS = "approvals"  # approve/reject tickets
CAP_KILL = "kill"  # kill/lift orders
CAP_AUDIT = "audit"  # audit() returns the decision log
CAP_CLOCK = "clock"  # set_clock controls time-based gates
CAP_DISPATCH_FAILURE = "dispatch-failure-injection"  # inject_dispatch_failure()
# v0.4 CS-017: decision TTL + volatile-gate re-validation are wired, with the
# REQUIRED TCK freshness config — default TTL 24 hours, irreversible TTL 30
# minutes (fixture semantics, like the registered functions in docs/12 §3) —
# and update_named_set() can change a named set between decision and dispatch.
CAP_FRESHNESS = "freshness"
# v0.4 CS-018: the scope predicate is re-asserted at dispatch (either declared
# form — transactional or window pre-dispatch re-resolve; the TCK observes only
# the outcome: the effect does not land and the settle reason is "scope-lost").
CAP_SCOPE_REASSERT = "scope-reassert"
# v0.5 CS-023: multi-operation SIF intents are decided atomically —
# ``submit_batch`` submits one batch and reports the batch verdict plus the
# per-operation results.
CAP_BATCH = "batch"
# v0.5 CS-020: connector digest pinning is verified at policy load and at
# dispatch. ``connector_digest``/``tamper_connector`` let the TCK pin the real
# artifact and then simulate its supply-chain replacement; a dispatch-time
# mismatch MUST settle with reason ``connector-digest-mismatch`` (normative,
# like the v0.4 settle reasons).
CAP_DIGEST = "digest-pinning"

ALL_CAPABILITIES = frozenset(
    {
        CAP_LINT_WARNINGS,
        CAP_STAGING,
        CAP_APPROVALS,
        CAP_KILL,
        CAP_AUDIT,
        CAP_CLOCK,
        CAP_DISPATCH_FAILURE,
        CAP_FRESHNESS,
        CAP_SCOPE_REASSERT,
        CAP_BATCH,
        CAP_DIGEST,
    }
)


@dataclass(frozen=True)
class LoadResult:
    """Outcome of loading a registry+policy pair. A policy with ERROR-level
    findings MUST NOT load (``ok=False``) — the gateway refuses, never falls
    back to a permissive default (RFC §13)."""

    ok: bool
    errors: Sequence[str] = ()
    warnings: Sequence[str] = ()


@dataclass(frozen=True)
class SubmitResult:
    """Normalized outcome of one submitted operation.

    ``decision`` is one of ``allow | hold | deny | halt`` (RFC §2).
    ``ticket`` identifies a staged/held action (staging/approvals capability).
    ``rows`` carries an ``observe``'s result (already scope-filtered).
    """

    decision: str
    ticket: str | None = None
    rows: Sequence[Mapping[str, Any]] | None = None
    reason: str = ""


@dataclass(frozen=True)
class BatchSubmitResult:
    """Normalized outcome of one submitted batch (v0.5 CS-023).

    ``decision`` is the batch verdict: the first refusing operation's
    ``deny``/``halt``, else ``hold`` when any operation held, else ``allow``.
    ``failing_index`` names the refusing operation (the SIF §6 pointer
    ``operations[i]``); ``None`` when the batch committed. ``results`` carries
    one ``SubmitResult`` per operation, in submission order.
    """

    decision: str
    failing_index: int | None = None
    results: Sequence[SubmitResult] = ()


@dataclass(frozen=True)
class AuditEntry:
    """The normalized audit shape the TCK asserts on (a subset of RFC §11)."""

    decision: str  # allow | hold | deny | halt
    resource: str | None
    action: str | None
    outcome: str  # success | failure | not_executed | (impl-specific detail)
    # The deciding rule/gate or settle reason (RFC §11) — the v0.4 reasons
    # ("stale-decision", "stale-guard:<gate>", "scope-lost") are normative, so
    # a driver claiming CAP_FRESHNESS / CAP_SCOPE_REASSERT MUST populate this
    # for cancelled/failed settle records. Empty otherwise is acceptable.
    reason: str = ""


@dataclass(frozen=True)
class Operation:
    """One intent the TCK submits. ``target`` selects an existing row by id
    (the driver resolves it below the model — the TCK never passes internal
    row objects); ``sink`` is the requested disclosure destination; ``context``
    is ambient state (e.g. ``breakGlass``) supplied by the *session*, not the
    agent (invariant 3)."""

    resource: str
    action: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict)
    target: str | None = None
    sink: str | None = None
    context: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TckActor:
    """The authenticated principal the TCK submits as."""

    id: str
    roles: frozenset[str] = frozenset()
    claims: Mapping[str, Any] = field(default_factory=dict)


class ConformanceDriver(Protocol):
    """What an implementation provides to run the TCK (docs/12 §2)."""

    def capabilities(self) -> frozenset[str]:
        """The subset of ``ALL_CAPABILITIES`` this driver supports."""
        ...

    def load(self, registry_yaml: str, policy_yaml: str) -> LoadResult:
        """(Re)configure the gateway under test with this registry + policy.
        Resets all state (rows, counters, staged actions, kills, audit)."""
        ...

    def set_clock(self, now: datetime) -> None:
        """Pin the injected clock every time-based gate reads (CAP_CLOCK)."""
        ...

    def seed(self, resource: str, rows: Sequence[Mapping[str, Any]]) -> None:
        """Load rows into the store behind the entity's connector."""
        ...

    def submit(
        self, actor: TckActor, session_id: str, op: Operation
    ) -> SubmitResult:
        """Submit one operation as ``actor`` (identity from the transport —
        the payload must not be able to influence it)."""
        ...

    def approve(self, ticket: str, approver_id: str) -> bool:
        """Record an approval; ``False`` if refused (e.g. self-approval on
        dual-authorization). (CAP_APPROVALS)"""
        ...

    def reject(self, ticket: str, approver_id: str) -> bool:
        """Reject a held action. (CAP_APPROVALS)"""
        ...

    def dispatch_once(self) -> int:
        """Synchronously run the dispatch worker until no staged action is
        claimable; return how many settled. (CAP_STAGING)"""
        ...

    def effects(self) -> Sequence[Mapping[str, Any]]:
        """Every external effect that actually left the gateway, in order:
        ``{"resource": ..., "action": ..., "data": {...}}``."""
        ...

    def kill(
        self,
        *,
        scope: str,
        agent: str | None = None,
        session_id: str | None = None,
        resource: str | None = None,
        action: str | None = None,
        issued_by: str = "tck-operator",
    ) -> str:
        """Issue a kill order (``scope`` ∈ global|agent|session|action_class);
        returns its id. (CAP_KILL)"""
        ...

    def lift(self, kill_id: str) -> None:
        """Lift a kill order. (CAP_KILL)"""
        ...

    def audit(self) -> Sequence[AuditEntry]:
        """Every audit record written since ``load``, in order. (CAP_AUDIT)"""
        ...

    def inject_dispatch_failure(self, action: str) -> None:
        """Make the next dispatch of ``action`` fail at the connector —
        exercises compensation staging. (CAP_DISPATCH_FAILURE)"""
        ...

    def update_named_set(self, name: str, values: Sequence[str]) -> None:
        """Replace a registry named set's values at runtime — simulates a
        sanctions-list update landing between decision and dispatch, so the
        TCK can prove volatile gates are re-validated at claim (v0.4 CS-017).
        (CAP_FRESHNESS)"""
        ...

    def submit_batch(
        self, actor: TckActor, session_id: str, ops: Sequence[Operation]
    ) -> BatchSubmitResult:
        """Submit one multi-operation SIF batch as ``actor`` (v0.5 CS-023) —
        decided atomically, same identity rule as ``submit``. (CAP_BATCH)"""
        ...

    def connector_digest(self, name: str) -> str:
        """The content digest (``sha256:<64 hex>``) of the artifact currently
        implementing connector ``name`` — computed exactly the way the gateway
        verifies a registry pin, so the TCK can author a registry that pins the
        real implementation. (CAP_DIGEST)"""
        ...

    def tamper_connector(self, name: str) -> None:
        """Swap/modify the implementation of connector ``name`` in place,
        WITHOUT reloading policy, so its artifact no longer matches any pinned
        digest — the supply-chain replacement CS-020 defends against. Takes
        effect for subsequent dispatches until the next ``load``. (CAP_DIGEST)"""
        ...
