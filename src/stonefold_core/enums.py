# SPDX-License-Identifier: Apache-2.0
"""Frozen vocabulary enums.

Implements RFC §3 (action kinds), §5 (governance attribute value sets) and the
``Decision``/``Outcome`` types from design §2. These value sets are *frozen* in
v0.1 (RFC §13 / §15): code MUST NOT add kinds, attribute names, or decision
states. Domains may extend the *value set* of ``resultSensitivity`` only, which
is why that one attribute is modelled as a plain ``str`` (see ``models.py``).
"""

from __future__ import annotations

from enum import Enum


class Kind(str, Enum):
    """The five fixed action categories (RFC §3). Declared per action in the
    registry, never chosen by the policy or the agent."""

    OBSERVE = "observe"
    ASSESS = "assess"
    RECORD = "record"
    EFFECT = "effect"
    TRANSITION = "transition"


class Decision(str, Enum):
    """The gateway's verdict for an attempted action (RFC §2, design §2)."""

    ALLOW = "allow"
    HOLD = "hold"
    DENY = "deny"
    HALT = "halt"


class Outcome(str, Enum):
    """A single gate's result (RFC §7, design §2). A gate that needs a human
    returns ``HOLD``; a policy failure is ``FAIL``. A *raised* exception is a
    dependency failure (→ ``failureMode``), never a policy decision."""

    PASS = "pass"
    FAIL = "fail"
    HOLD = "hold"


class RetryClass(str, Enum):
    """The retry class every deny/hold reason code declares (RFC §11, v0.6
    CS-029): what an iterating agent should do with the refusal. An
    undeclared/unknown code defaults to ``TERMINAL`` — the safe direction is to
    stop retrying."""

    RETRYABLE = "retryable"  # the defect is in the intent; fix it and resubmit
    TERMINAL = "terminal"  # nothing the agent can fix; do not resubmit
    ESCALATE = "escalate"  # stop and surface to a human on the AGENT's side


class FeedbackLevel(str, Enum):
    """What the AGENT receives on a deny/hold (RFC §11, v0.6 CS-030). The audit
    record always carries everything — redact on return, never on write."""

    CODE = "code"  # reason code + retry class only
    CODE_FIELDS = "code+fields"  # + which intent fields failed; never record-side values
    CODE_EVIDENCE = "code+evidence"  # the full comparison; trusted internal loops only


class Reversibility(str, Enum):
    """How recoverable an action is (RFC §5). Drives approval/gate strength."""

    REVERSIBLE = "reversible"
    COMPENSABLE = "compensable"
    IRREVERSIBLE = "irreversible"


class Emission(str, Enum):
    """Whether the act transmits into the world even while "just looking"
    (RFC §5). ``emits`` forces observe-looking sensing into effect handling."""

    NONE = "none"
    EMITS = "emits"


class OperativeForce(str, Enum):
    """Whether parties treat the result as authoritative and act on it
    (RFC §5) — a DNR, a target designation."""

    NONE = "none"
    LOW = "low"
    HIGH = "high"


class Explainability(str, Enum):
    """Whether the action must carry a recorded rationale (RFC §5; typically
    an ``assess``)."""

    NONE = "none"
    REQUIRED = "required"
