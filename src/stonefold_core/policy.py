"""The typed policy document model (RFC §6 top-level keys).

This is the *parsed* policy — one step before the compiled matcher (see
``compiler.py``). Structural validity (shapes, enums) is enforced here by
pydantic; cross-references and semantic rules (RFC §13) are checked by the
linter; deny-wins/most-specific authorization is the compiler's job.

Gate *configs* are kept as raw mappings at this layer (``gates`` /
``standing.enables``); the typed gate models and the gate engine arrive in M2.
The linter reads the raw gate configs to apply RFC §13 checks.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Union

from pydantic import BaseModel, ConfigDict, Field
from typing_extensions import Literal

from stonefold_core.enums import Kind

# A target under a kind: '*' | [resources-or-actions] | {Resource: [actions]}.
Targets = Union[Literal["*"], list[str], dict[str, list[str]]]

# One entry in an allow/deny list, e.g. {"observe": ["Customer"]}.
PermissionMap = dict[Kind, Targets]


class FailureMode(str, Enum):
    CLOSED = "closed"
    OPEN = "open"


class AuditLevel(str, Enum):
    NONE = "none"
    BASIC = "basic"
    FULL = "full"


class Defaults(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    failureMode: FailureMode = FailureMode.CLOSED
    audit: AuditLevel | None = None
    killable: bool | None = None


class Standing(BaseModel):
    """A context-conditioned authorization (RFC §7.15)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    when: str
    enables: PermissionMap


class Policy(BaseModel):
    """A parsed ACP policy document (RFC §6)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    apiVersion: str | None = None
    agent: str
    extends: tuple[str, ...] = ()
    defaults: Defaults = Field(default_factory=Defaults)
    allow: tuple[PermissionMap, ...]
    deny: tuple[PermissionMap, ...] = ()
    scope: dict[str, str] = Field(default_factory=dict)
    # gate key -> {gate-name: config}. Configs stay raw until M2.
    gates: dict[str, dict[str, Any]] = Field(default_factory=dict)
    standing: tuple[Standing, ...] = ()
    killable: bool | None = None
    audit: AuditLevel | None = None

    @property
    def effective_killable(self) -> bool:
        """RFC §9: killable SHOULD default to true for non-trivial agents."""
        if self.killable is not None:
            return self.killable
        if self.defaults.killable is not None:
            return self.defaults.killable
        return True

    @property
    def effective_audit(self) -> AuditLevel:
        return self.audit or self.defaults.audit or AuditLevel.FULL
