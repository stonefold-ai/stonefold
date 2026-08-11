# SPDX-License-Identifier: Apache-2.0
"""The typed policy document model (spec §6 top-level keys).

This is the *parsed* policy — one step before the compiled matcher (see
``compiler.py``). Structural validity (shapes, enums) is enforced here by
pydantic; cross-references and semantic rules (spec §13) are checked by the
linter; deny-wins/most-specific authorization is the compiler's job.

Gate *configs* are kept as raw mappings at this layer (``gates`` /
``standing.enables``); the typed gate models and the gate engine arrive in M2.
The linter reads the raw gate configs to apply spec §13 checks.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Union

from pydantic import BaseModel, ConfigDict, Field
from typing_extensions import Literal

from stonefold_core.enums import EnforcementMode, Kind

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
    # advisory profile. Policy-wide by design: an agent's policy is
    # advisory or enforcing, whole. A document that enforced some of its rules
    # and advised on others would not produce a counterfactual — the enforced
    # refusals change what the actor does next, and the record could no longer
    # say what the traffic looks like unobserved.
    #
    # This is one of TWO keys. Declaring it here makes the mode reviewable,
    # versioned and lintable with the rest of the policy, but it does NOT take
    # effect on its own: the deployment must also permit advisory for this agent
    # (see ``load_policy``). A single field in a policy file that silently turns
    # enforcement off would be a bypass wearing the product's own clothes.
    enforcement: EnforcementMode = EnforcementMode.ENFORCED


class Standing(BaseModel):
    """A context-conditioned authorization (spec §7.15)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    when: str
    enables: PermissionMap


class Policy(BaseModel):
    """A parsed Stele policy document (spec §6)."""

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
        """spec §9: killable SHOULD default to true for non-trivial agents."""
        if self.killable is not None:
            return self.killable
        if self.defaults.killable is not None:
            return self.defaults.killable
        return True

    @property
    def effective_audit(self) -> AuditLevel:
        return self.audit or self.defaults.audit or AuditLevel.FULL

    @property
    def effective_enforcement(self) -> EnforcementMode:
        """What this policy asks the deployment to do with its verdicts.

        Enforcing unless the document says otherwise — the safe reading of
        silence, and the reading every policy written before the advisory
        profile existed keeps.
        """
        return self.defaults.enforcement

    @property
    def is_advisory(self) -> bool:
        return self.defaults.enforcement is EnforcementMode.ADVISORY
