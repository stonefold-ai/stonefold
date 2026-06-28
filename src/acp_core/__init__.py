"""acp_core — the pure trust kernel of the ACP Gateway.

No I/O, no LLM, no framework imports (CLAUDE.md coding conventions; ADR docs/03).
Re-exports the value model, registry, audit seam, and the enforcement pipeline.
"""

from __future__ import annotations

from acp_core.audit import AuditSink, FallbackAuditSink, InMemoryAuditSink, build_record
from acp_core.failure import Ok, Unavailable, guard, should_fail_closed
from acp_core.enums import (
    Decision,
    Emission,
    Explainability,
    Kind,
    OperativeForce,
    Outcome,
    Reversibility,
)
from acp_core.models import (
    Actor,
    Attributes,
    AuditRecord,
    Compensation,
    EvalResult,
    GateResult,
    RawCall,
    ResolvedAction,
    Session,
)
from acp_core.compiler import AuthzResult, CompiledPolicy, KindMatcher, MatchSpecificity
from acp_core.condition import (
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
from acp_core.gating import ApprovalSpec, GateEngine, GateOutcome, RequestEnv
from acp_core.connector import (
    Connector,
    ConnectorCancelled,
    ConnectorRegistry,
    ConnectorResult,
    Connectors,
)
from acp_core.kill import (
    KillOrder,
    KillScope,
    KillScopeKind,
    KillStore,
    KillTarget,
    is_killed,
    order_matches,
    scope_matches,
)
from acp_core.outbox import (
    ApprovalError,
    OutboxError,
    OutboxStore,
    PendingAction,
    PendingState,
    SelfApprovalError,
    UnknownTicketError,
)
from acp_core.scope import (
    AttributeScope,
    ScopePredicate,
    ScopeRegistry,
    ScopeResolver,
    default_scope_registry,
    make_scope_resolver,
)
from acp_core.linter import LintFinding, LintReport, PolicyError, Severity, lint
from acp_core.loader import SchemaError, load_policy, merge_extends, validate_only
from acp_core.pipeline import enforce
from acp_core.policy import (
    AuditLevel,
    Defaults,
    FailureMode,
    PermissionMap,
    Policy,
    Standing,
    Targets,
)
from acp_core.registry import (
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
    # outbox seam
    "OutboxStore",
    "PendingAction",
    "PendingState",
    "OutboxError",
    "ApprovalError",
    "SelfApprovalError",
    "UnknownTicketError",
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
]
