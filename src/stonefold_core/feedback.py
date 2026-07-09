"""Agent feedback visibility (RFC §11, v0.6 CS-030).

What the *agent* receives on a deny/hold is a declared choice — ``code`` |
``code+fields`` (default) | ``code+evidence`` — applied by the TRANSPORT on the
return path. The pipeline's own ``EvalResult`` is always full and the audit
record is written from it before any redaction: **redact on return, never on
write**. Pure module (no I/O); redaction is a deterministic projection, so
invariant 1 is untouched.

Reason codes remain an oracle even at ``code`` (each probe maps one policy
wall). The countermeasure is detection, not blunting: the admin surface exposes
deny-rate and reason-code distribution per principal — a converging loop and a
mapping loop look different in that data.
"""

from __future__ import annotations

from stonefold_core.enums import FeedbackLevel
from stonefold_core.models import BatchResult, EvalResult, GateResult

DEFAULT_FEEDBACK = FeedbackLevel.CODE_FIELDS


def parse_feedback(raw: object) -> FeedbackLevel:
    """The ``feedback:`` gate-set key, defaulting per CS-030. Unknown values
    fall back to the DEFAULT (never to the leakier ``code+evidence``)."""
    try:
        return FeedbackLevel(str(raw))
    except ValueError:
        return DEFAULT_FEEDBACK


def _redact_gate(g: GateResult) -> GateResult:
    """The ``code+fields`` view of one gate result: gate, outcome, code, retry
    class, source, and the intent-side ``fields`` — with the prose ``reason``
    and check-supplied ``evidence`` stripped (both may carry record-side or
    policy-constant values)."""
    if not g.reason and g.evidence is None:
        return g
    return g.model_copy(update={"reason": "", "evidence": None})


def agent_view(result: EvalResult, level: FeedbackLevel | None = None) -> EvalResult:
    """The agent-facing projection of a full ``EvalResult`` (CS-030).

    ``level`` defaults to the result's stamped policy level. ``code+evidence``
    is the identity; ``code+fields`` strips prose reasons and evidence from the
    gate trace; ``code`` strips the trace and the scope description entirely.
    The decision, rule, reason code, retry class, ticket, and connector output
    always pass through — they are the loop's signal.
    """
    lvl = level if level is not None else result.feedback
    if lvl is FeedbackLevel.CODE_EVIDENCE:
        return result
    if lvl is FeedbackLevel.CODE:
        if not result.gates and not result.scope_applied:
            return result
        return result.model_copy(update={"gates": (), "scope_applied": ()})
    redacted = tuple(_redact_gate(g) for g in result.gates)
    if redacted == result.gates:
        return result
    return result.model_copy(update={"gates": redacted})


def agent_view_batch(batch: BatchResult) -> BatchResult:
    """Per-operation redaction of a batch verdict (CS-023 × CS-030)."""
    redacted = tuple(agent_view(r) for r in batch.results)
    if redacted == batch.results:
        return batch
    return batch.model_copy(update={"results": redacted})
