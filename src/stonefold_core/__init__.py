"""stonefold_core — the pure trust kernel of the Stonefold Gateway.

No I/O, no LLM, no framework imports (CLAUDE.md coding conventions; ADR docs/03).
Re-exports the value model, registry, audit seam, and the enforcement pipeline.
"""

from __future__ import annotations

from stonefold_core.audit import AuditSink, FallbackAuditSink, InMemoryAuditSink, build_record
from stonefold_core.failure import Ok, Unavailable, guard, should_fail_closed
from stonefold_core.freshness import (
    STALE_DECISION,
    VOLATILE_GATES,
    DispatchRevalidator,
    FreshnessConfig,
    stale_guard_reason,
)
from stonefold_core.enums import (
    Decision,
    Emission,
    Explainability,
    Kind,
    OperativeForce,
    Outcome,
    Reversibility,
)
from stonefold_core.models import (
    Actor,
    Attributes,
    AuditRecord,
    Compensation,
    BatchResult,
    EvalResult,
    GateResult,
    RawCall,
    ResolvedAction,
    Session,
)
from stonefold_core.compiler import AuthzResult, CompiledPolicy, KindMatcher, MatchSpecificity
from stonefold_core.digest import (
    DIGEST_MISMATCH,
    DigestMismatch,
    DigestMismatchError,
    artifact_digest,
    assert_connector_digests,
    digest_matches,
    pinned_connector_mismatch,
    verify_connector_digests,
)
from stonefold_core.condition import (
    ConditionError,
    ConditionRuntimeError,
    EvalContext,
    MissingValueError,
    evaluate as evaluate_condition,
    evaluate_str,
    make_window,
    parse as parse_condition,
    parse_and_validate,
    validate as validate_condition,
)
from stonefold_core.gating import (
    ApprovalSpec,
    GateEngine,
    GateOutcome,
    ReleaseContract,
    RequestEnv,
)
from stonefold_core.connector import (
    SCOPE_LOST,
    Connector,
    ConnectorCancelled,
    ConnectorRegistry,
    ConnectorResult,
    Connectors,
    ScopeCapability,
    ScopeLostError,
    ScopeReassertion,
    TransactionalDispatch,
    scope_capability_of,
)
from stonefold_core.kill import (
    KillOrder,
    KillScope,
    KillScopeKind,
    KillStore,
    KillTarget,
    is_killed,
    order_matches,
    scope_matches,
)
from stonefold_core.outbox import (
    ApprovalError,
    OutboxError,
    OutboxStore,
    PendingAction,
    PendingState,
    SelfApprovalError,
    StaleCheck,
    UnknownTicketError,
    cancellation_record,
)
from stonefold_core.scope import (
    AttributeScope,
    ScopePredicate,
    ScopeRegistry,
    ScopeResolver,
    default_scope_registry,
    make_scope_resolver,
)
from stonefold_core.linter import LintFinding, LintReport, PolicyError, Severity, lint
from stonefold_core.loader import SchemaError, load_policy, merge_extends, validate_only
from stonefold_core.pipeline import enforce, enforce_batch
from stonefold_core.policy import (
    AuditLevel,
    Defaults,
    FailureMode,
    PermissionMap,
    Policy,
    Standing,
    Targets,
)
from stonefold_core.registry import (
    ActionDef,
    InMemoryRegistry,
    Registry,
    RegistryFile,
    ResourceDef,
    UnknownActionError,
    load_registry,
)

__all__ = [
    # enums
    "Kind",
    "Decision",
    "Outcome",
    "Reversibility",
    "Emission",
    "OperativeForce",
    "Explainability",
    # models
    "Attributes",
    "RawCall",
    "ResolvedAction",
    "Compensation",
    "Actor",
    "Session",
    "GateResult",
    "BatchResult",
    "EvalResult",
    "AuditRecord",
    # registry
    "Registry",
    "InMemoryRegistry",
    "RegistryFile",
    "ResourceDef",
    "ActionDef",
    "UnknownActionError",
    "load_registry",
    # audit
    "AuditSink",
    "InMemoryAuditSink",
    "FallbackAuditSink",
    "build_record",
    # failure mode (RFC §10, design §12)
    "Ok",
    "Unavailable",
    "guard",
    "should_fail_closed",
    # policy model
    "Policy",
    "Defaults",
    "Standing",
    "FailureMode",
    "AuditLevel",
    "PermissionMap",
    "Targets",
    # compiler
    "CompiledPolicy",
    "AuthzResult",
    "KindMatcher",
    "MatchSpecificity",
    # connector digest pinning (CS-020)
    "DIGEST_MISMATCH",
    "DigestMismatch",
    "DigestMismatchError",
    "artifact_digest",
    "digest_matches",
    "pinned_connector_mismatch",
    "verify_connector_digests",
    "assert_connector_digests",
    # condition engine
    "parse_condition",
    "validate_condition",
    "parse_and_validate",
    "evaluate_condition",
    "evaluate_str",
    "make_window",
    "EvalContext",
    "ConditionError",
    "ConditionRuntimeError",
    "MissingValueError",
    # gating seam
    "GateEngine",
    "GateOutcome",
    "RequestEnv",
    "ApprovalSpec",
    "ReleaseContract",
    # outbox seam
    "OutboxStore",
    "PendingAction",
    "PendingState",
    "OutboxError",
    "ApprovalError",
    "SelfApprovalError",
    "StaleCheck",
    "UnknownTicketError",
    "cancellation_record",
    # decision freshness (v0.4 CS-017)
    "FreshnessConfig",
    "DispatchRevalidator",
    "VOLATILE_GATES",
    "STALE_DECISION",
    "stale_guard_reason",
    # scope injection
    "ScopePredicate",
    "AttributeScope",
    "ScopeRegistry",
    "ScopeResolver",
    "default_scope_registry",
    "make_scope_resolver",
    # connector seam
    "Connector",
    "ConnectorCancelled",
    "ConnectorRegistry",
    "ConnectorResult",
    "Connectors",
    # scope no-race (v0.4 CS-018)
    "SCOPE_LOST",
    "ScopeCapability",
    "ScopeLostError",
    "ScopeReassertion",
    "TransactionalDispatch",
    "scope_capability_of",
    # kill-switch seam
    "KillScope",
    "KillScopeKind",
    "KillOrder",
    "KillTarget",
    "KillStore",
    "scope_matches",
    "order_matches",
    "is_killed",
    # linter / loader
    "lint",
    "LintReport",
    "LintFinding",
    "Severity",
    "PolicyError",
    "load_policy",
    "validate_only",
    "merge_extends",
    "SchemaError",
    # pipeline
    "enforce",
    "enforce_batch",
]
