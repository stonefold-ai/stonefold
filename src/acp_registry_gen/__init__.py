"""acp_registry_gen — draft a registry from what the integrator already has.

Authoring-time only (docs/06 §9): SQL DDL, an OpenAPI spec, or an MCP tool
list in; a DRAFT v1.x-authoring-format registry out, every guess marked
``TODO(review)``. A human reviews, completes, and signs the result — the
generator is never part of the enforcement path.
"""

from acp_registry_gen.emit import emit_yaml, validate_registry_yaml
from acp_registry_gen.importers import draft_from_mcp_tools, draft_from_openapi
from acp_registry_gen.model import DraftAction, DraftEntity, DraftProperty, DraftRegistry
from acp_registry_gen.sql import draft_from_sql
from acp_registry_gen.stubs import (
    ConnectorStub,
    StubPlan,
    emit_stubs,
    plan_from_draft,
    plan_from_registry,
    validate_stub_code,
)

__all__ = [
    "DraftAction",
    "DraftEntity",
    "DraftProperty",
    "DraftRegistry",
    "draft_from_mcp_tools",
    "draft_from_openapi",
    "draft_from_sql",
    "emit_yaml",
    "validate_registry_yaml",
    # handler-stub generation (G1)
    "ConnectorStub",
    "StubPlan",
    "emit_stubs",
    "plan_from_draft",
    "plan_from_registry",
    "validate_stub_code",
]
