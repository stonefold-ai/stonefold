# SPDX-License-Identifier: Apache-2.0
"""The figures behind an Advisory Report, computed from the audit and nothing else.

Every number a report states comes from here, and everything here comes from
records the gateway wrote. That constraint is the point: a report generator that
can compute a figure its audit cannot produce is a generator that will one day
state one.

The refusals matter as much as the counts, and they are enforced in code rather
than left to whoever writes the prose:

* a figure is never averaged across enforcement modes (``MixedDatasetError``);
* would-refuse and would-hold counts run over JUDGED records only — an unjudged
  record can carry advice too, and that advice is a fail-closed reflex rather
  than a judgement about the action;
* nothing carries a row value out of the audit: parameters are reduced to their
  shape before they can reach a page;
* a partial measurement stays marked partial, and a measurement that could not
  be made is absent rather than zero.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from stonefold_core.enums import Coverage, Decision, EnforcementMode
from stonefold_core.models import AuditRecord

# Causes of an unjudged record, in the customer's terms. The gateway's rules are
# the source; these are what they mean to somebody deciding whether to enforce.
CAUSE_UNDECLARED = "action not declared in the registry"
CAUSE_UNMAPPED = "tool not covered by the interception mapping"
CAUSE_DEPENDENCY = "a store the decision needed was unreachable"
CAUSE_OTHER = "other"


class MixedDatasetError(ValueError):
    """The dataset spans both enforcement modes for one agent.

    Refusing is the whole point. An advisory figure averaged with an enforced
    one describes no deployment that ever ran, and the reader has no way to see
    it happened.
    """


class NotAdvisoryError(ValueError):
    """The dataset carries no advisory records for this agent, so there is no
    counterfactual to report."""


def _shape(value: Any) -> str:
    """A parameter reduced to its shape. The customer already has their data;
    our copy of it is a liability rather than a service."""
    if isinstance(value, bool):
        return "<boolean>"
    if isinstance(value, int):
        return "<integer>"
    if isinstance(value, float):
        return "<decimal>"
    if isinstance(value, Mapping):
        return "<object>"
    if isinstance(value, (list, tuple)):
        return f"<list[{len(value)}]>"
    if value is None:
        return "<null>"
    return "<string>"


def shape_of(parameters: Mapping[str, Any]) -> dict[str, str]:
    return {key: _shape(value) for key, value in sorted(parameters.items())}


def _cause(record: AuditRecord) -> str:
    rule = record.rule or ""
    advised_rule = record.advised.rule if record.advised is not None else ""
    if rule == "unknown-action" or advised_rule == "unknown-action":
        return CAUSE_UNDECLARED
    if advised_rule == "unmapped-tool":
        return CAUSE_UNMAPPED
    if rule.endswith("-unavailable") or advised_rule.endswith("-unavailable"):
        return CAUSE_DEPENDENCY
    return CAUSE_OTHER


@dataclass(frozen=True)
class CoverageFigures:
    observed: int
    judged: int
    unjudged: int
    by_cause: tuple[tuple[str, int], ...]

    @property
    def ratio(self) -> float | None:
        """Judged share, or ``None`` where there is nothing to divide. Rounded
        DOWN at one decimal: the number is a claim about our own reach, and the
        safe direction to be wrong about it is downwards."""
        if self.observed == 0:
            return None
        return int(self.judged * 1000 / self.observed) / 10


@dataclass(frozen=True)
class RuleLine:
    rule: str
    decision: str
    count: int
    actions: tuple[str, ...]
    examples: tuple[Mapping[str, str], ...]


@dataclass(frozen=True)
class WouldHaveFigures:
    """What the policy would have done, over judged records only."""

    allowed: int
    refused: int
    held: int
    halted: int
    by_rule: tuple[RuleLine, ...]
    # A batch refuses atomically, so the unit is the batch, not its operations.
    batches_refused: int
    # An item-bearing call applies whole; the calls and the items inside them
    # are different numbers and neither may stand in for the other.
    item_calls: int
    item_refusals: int


@dataclass(frozen=True)
class QuestionFigures:
    """Human attention: distinct questions, not raw holds."""

    distinct: int
    total_holds: int
    busiest: tuple[str, int] | None
    # Holds that carry no question identity (an approval-shaped hold is a
    # distinct question per intent by design, so it never collapses).
    unkeyed: int

    @property
    def total_questions(self) -> int:
        """Questions a human would actually face.

        Keyed holds collapse: the same question asked twenty times is one queue
        item. Approval-shaped holds do NOT collapse — two different payments
        awaiting approval are two questions by design — so each is its own. The
        staffing claim is the sum, and reporting only the keyed half would have
        told a customer with four pending approvals that they had none.
        """
        return self.distinct + self.unkeyed

    @property
    def repeats_per_question(self) -> float | None:
        if self.total_questions == 0:
            return None
        return round(self.total_holds / self.total_questions, 1)


@dataclass(frozen=True)
class ScopeFigures:
    reads: int
    rows_returned: int
    rows_removed: int
    widest: tuple[str, int] | None
    partial_reads: int
    unmeasured: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class Disclosures:
    kill_orders: int
    kill_unavailable: int
    first: datetime | None
    last: datetime | None


@dataclass(frozen=True)
class WorksheetLine:
    """One would-refuse the customer has to rule on. The generator cannot know
    whether a refusal was right; §4 of the report is a question, not an
    answer."""

    rule: str
    resource: str | None
    action: str | None
    count: int


@dataclass(frozen=True)
class Report:
    agent: str
    coverage: CoverageFigures
    would_have: WouldHaveFigures
    questions: QuestionFigures
    scope: ScopeFigures
    disclosures: Disclosures
    worksheet: tuple[WorksheetLine, ...]
    # Present only where the customer supplied verdicts; absent, never zero.
    false_positives: int | None
    reviewed: int | None
    # Present only where downstream outcome data was joined.
    exclusive: int | None


def _judged(records: Sequence[AuditRecord]) -> list[AuditRecord]:
    return [r for r in records if r.coverage is Coverage.JUDGED]


def _coverage(records: Sequence[AuditRecord]) -> CoverageFigures:
    unjudged = [r for r in records if r.coverage is Coverage.UNJUDGED]
    causes = Counter(_cause(r) for r in unjudged)
    return CoverageFigures(
        observed=len(records),
        judged=len(records) - len(unjudged),
        unjudged=len(unjudged),
        by_cause=tuple(sorted(causes.items(), key=lambda kv: (-kv[1], kv[0]))),
    )


def _would_have(records: Sequence[AuditRecord]) -> WouldHaveFigures:
    judged = _judged(records)
    advised = [r for r in judged if r.advised is not None]
    by_rule: dict[tuple[str, str], list[AuditRecord]] = {}
    for record in advised:
        assert record.advised is not None
        by_rule.setdefault((record.advised.rule, record.advised.decision.value), []).append(record)
    lines = tuple(
        RuleLine(
            rule=rule,
            decision=decision,
            count=len(rows),
            actions=tuple(sorted({r.action or "" for r in rows if r.action})),
            examples=tuple(shape_of(r.parameters) for r in rows[:3]),
        )
        for (rule, decision), rows in sorted(
            by_rule.items(), key=lambda kv: (-len(kv[1]), kv[0])
        )
    )
    # A batch's refusal is one event carried on every operation's record.
    batches = {
        (r.correlationId, r.batchAdvice.get("failingIndex"))
        for r in judged
        if r.batchAdvice is not None and r.batchAdvice.get("wouldRefuse")
    }
    item_records = [r for r in judged if r.itemAdvice is not None]
    return WouldHaveFigures(
        allowed=sum(1 for r in judged if r.advised is None),
        refused=sum(1 for r in advised if r.advised and r.advised.decision is Decision.DENY),
        held=sum(1 for r in advised if r.advised and r.advised.decision is Decision.HOLD),
        halted=sum(1 for r in advised if r.advised and r.advised.decision is Decision.HALT),
        by_rule=lines,
        batches_refused=len(batches),
        item_calls=len(item_records),
        item_refusals=sum(
            int(r.itemAdvice.get("wouldRefuse", 0)) for r in item_records if r.itemAdvice
        ),
    )


def _questions(records: Sequence[AuditRecord]) -> QuestionFigures:
    holds = [
        r for r in _judged(records)
        if r.advised is not None and r.advised.decision is Decision.HOLD
    ]
    keys = Counter(
        r.advised.dedupe_key for r in holds
        if r.advised is not None and r.advised.dedupe_key is not None
    )
    busiest = None
    if keys:
        key, count = keys.most_common(1)[0]
        # name the question by its action and code, never by its key
        example = next(
            r for r in holds if r.advised is not None and r.advised.dedupe_key == key
        )
        assert example.advised is not None
        busiest = (
            f"{example.resource}.{example.action} ({example.advised.reason_code})",
            count,
        )
    return QuestionFigures(
        distinct=len(keys),
        total_holds=len(holds),
        busiest=busiest,
        unkeyed=sum(
            1 for r in holds if r.advised is not None and r.advised.dedupe_key is None
        ),
    )


def _scope(records: Sequence[AuditRecord]) -> ScopeFigures:
    measured = []
    unmeasured: Counter[str] = Counter()
    for record in records:
        detail = record.scopeWouldRemove
        if detail is None:
            continue
        if detail.get("measured"):
            measured.append((record, detail))
        else:
            unmeasured[str(detail.get("reason", "unknown"))] += 1
    widest = None
    if measured:
        record, detail = max(measured, key=lambda pair: int(pair[1].get("removed", 0)))
        widest = (f"{record.resource}.{record.action}", int(detail.get("removed", 0)))
    return ScopeFigures(
        reads=len(measured) + sum(unmeasured.values()),
        rows_returned=sum(int(d.get("returned", 0)) for _r, d in measured),
        rows_removed=sum(int(d.get("removed", 0)) for _r, d in measured),
        widest=widest,
        partial_reads=sum(1 for _r, d in measured if d.get("partial")),
        unmeasured=tuple(sorted(unmeasured.items())),
    )


def _disclosures(records: Sequence[AuditRecord]) -> Disclosures:
    stamps = sorted(r.timestamp for r in records) if records else []
    return Disclosures(
        # an operator pulling the cord; `kill-unavailable` is NOT that, it is a
        # refusal nobody ordered, so it is counted apart and never presented as
        # use of the lever
        kill_orders=sum(1 for r in records if (r.rule or "").startswith("kill:")),
        kill_unavailable=sum(1 for r in records if r.rule == "kill-unavailable"),
        first=stamps[0] if stamps else None,
        last=stamps[-1] if stamps else None,
    )


def _worksheet(records: Sequence[AuditRecord]) -> tuple[WorksheetLine, ...]:
    counts: Counter[tuple[str, str | None, str | None]] = Counter()
    for record in _judged(records):
        if record.advised is not None and record.advised.decision is Decision.DENY:
            counts[(record.advised.rule, record.resource, record.action)] += 1
    return tuple(
        WorksheetLine(rule=rule, resource=resource, action=action, count=count)
        for (rule, resource, action), count in sorted(
            counts.items(), key=lambda kv: (-kv[1], kv[0])
        )
    )


def build_report(
    records: Sequence[AuditRecord],
    *,
    agent: str,
    verdicts: Mapping[str, str] | None = None,
    downstream_refused: Sequence[str] | None = None,
) -> Report:
    """Compute one agent's Advisory Report figures.

    ``agent`` is required rather than inferred: enforcement mode is a property
    of a policy and therefore of an agent, and a deployment may legitimately run
    one agent advisory while another enforces. Reporting is per agent for the
    same reason the mode is.

    ``verdicts`` maps a worksheet line's rule to the customer's ruling
    (``legitimate``/``correct``/``unsure``); without it the false-positive rate
    is ABSENT rather than zero. ``downstream_refused`` names the correlation ids
    the customer's own systems also refused; without it no exclusivity claim is
    computed at all.
    """
    mine = [r for r in records if r.agent == agent]
    modes = {r.enforcement for r in mine}
    if EnforcementMode.ADVISORY not in modes:
        raise NotAdvisoryError(
            f"no advisory records for agent {agent!r}: there is no counterfactual "
            "to report on"
        )
    if len(modes) > 1:
        enforced = [r for r in mine if r.enforcement is EnforcementMode.ENFORCED]
        raise MixedDatasetError(
            f"agent {agent!r} has {len(enforced)} enforced records and "
            f"{len(mine) - len(enforced)} advisory ones in this window "
            f"(first enforced at {min(r.timestamp for r in enforced).isoformat()}). "
            "No figure may span the two: one describes what was prevented and the "
            "other what would have been, and averaged they describe no deployment "
            "that ever ran."
        )

    worksheet = _worksheet(mine)
    reviewed = false_positives = None
    if verdicts is not None:
        ruled = [line for line in worksheet if line.rule in verdicts]
        reviewed = sum(line.count for line in ruled)
        false_positives = sum(
            line.count for line in ruled if verdicts[line.rule] == "legitimate"
        )
    exclusive = None
    if downstream_refused is not None:
        also = set(downstream_refused)
        exclusive = sum(
            1
            for r in _judged(mine)
            if r.advised is not None
            and r.advised.decision is Decision.DENY
            and r.correlationId not in also
        )
    return Report(
        agent=agent,
        coverage=_coverage(mine),
        would_have=_would_have(mine),
        questions=_questions(mine),
        scope=_scope(mine),
        disclosures=_disclosures(mine),
        worksheet=worksheet,
        false_positives=false_positives,
        reviewed=reviewed,
        exclusive=exclusive,
    )
