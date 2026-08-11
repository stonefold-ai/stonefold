# SPDX-License-Identifier: Apache-2.0
"""The Advisory Report, rendered.

The section order is not cosmetic. Coverage comes first because a report that
opens with catches and mentions coverage on page four is doing what this project
accuses everyone else of doing. What the policy would have got WRONG comes
before what it would have caught is celebrated, because a two-week pilot that
found no false positives either had a lucky fortnight or an under-inspected
report.

Two habits are enforced here rather than left to judgement:

* **absent, never zero** — a figure nobody supplied the inputs for says so, in
  the customer's words, instead of printing 0 and reading like a finding;
* **no prevention language** — nothing in an advisory window was prevented, so
  "would have" is the only tense available.
"""

from __future__ import annotations

from stonefold_report.figures import Report

_NO_PREVENTION = (
    "Nothing in this window was prevented. The gateway decided every action and "
    "acted on none of them, so every number below describes what a policy *would* "
    "have done, not what it stopped."
)


def _pct(value: float | None) -> str:
    return "not computable" if value is None else f"{value:.1f}%"


def render(report: Report) -> str:
    r = report
    out: list[str] = []
    add = out.append

    add(f"# Advisory Report — agent `{r.agent}`")
    add("")
    window = "no records"
    if r.disclosures.first is not None and r.disclosures.last is not None:
        window = (
            f"{r.disclosures.first.date().isoformat()} to "
            f"{r.disclosures.last.date().isoformat()}"
        )
    add(f"*Window: {window}. Mode: advisory throughout.*")
    add("")
    add(_NO_PREVENTION)
    add("")

    # --- 1. coverage, first -----------------------------------------------
    add("## 1. What we could see")
    add("")
    c = r.coverage
    add(f"- Actions observed: **{c.observed}**")
    add(f"- Judged by the policy: **{c.judged}** ({_pct(c.ratio)})")
    add(f"- Not judged: **{c.unjudged}**")
    if c.by_cause:
        add("")
        add("What we could not judge, and why:")
        add("")
        add("| Cause | Actions |")
        add("|---|---|")
        for cause, count in c.by_cause:
            add(f"| {cause} | {count} |")
        add("")
        add(
            "These are the actions a policy could not have applied to. Enforcement "
            "over the rest is a control with this shape of hole in it, which is "
            "worth deciding about deliberately rather than discovering later."
        )
    add("")

    # --- 2. what the policy would have done --------------------------------
    add("## 2. What the policy would have done")
    add("")
    w = r.would_have
    add("Counted over judged actions only.")
    add("")
    add(f"- Would have allowed: **{w.allowed}**")
    add(f"- Would have refused: **{w.refused}**")
    add(f"- Would have asked a human: **{w.held}**")
    if w.halted:
        add(f"- Would have halted: **{w.halted}**")
    if w.batches_refused:
        add(
            f"- Batches that would have been refused whole: **{w.batches_refused}** "
            "(a batch refuses atomically, so the unit here is the batch)"
        )
    if w.item_calls:
        add(
            f"- Multi-item calls that would have been partly refused: "
            f"**{w.item_calls}**, covering **{w.item_refusals}** items"
        )
    if w.by_rule:
        add("")
        add("| Rule | Verdict | Actions | Count |")
        add("|---|---|---|---|")
        for line in w.by_rule:
            actions = ", ".join(line.actions) or "—"
            add(f"| `{line.rule}` | would {line.decision} | {actions} | {line.count} |")
        add("")
        add("Examples carry the shape of each call, never its values:")
        add("")
        for line in w.by_rule[:3]:
            for example in line.examples[:1]:
                shape = ", ".join(f"{k}: {v}" for k, v in example.items()) or "no data"
                add(f"- `{line.rule}` — {shape}")
    add("")

    # --- 3. the human cost --------------------------------------------------
    add("## 3. What it would have cost in attention")
    add("")
    q = r.questions
    if q.total_holds == 0:
        add("No action in this window would have been sent to a human.")
    else:
        add(f"- Questions a person would have been asked: **{q.total_questions}**")
        add(f"- Times they would have been asked: **{q.total_holds}**")
        if q.repeats_per_question is not None and q.repeats_per_question > 1:
            add(f"- Repeats per question: **{q.repeats_per_question}×**")
        if q.busiest is not None:
            name, count = q.busiest
            add(f"- Busiest question: {name}, asked {count} times")
        if q.distinct and q.unkeyed:
            add(
                f"- Of those, **{q.distinct}** are repeatable checks that collapse "
                f"into one queue item each, and **{q.unkeyed}** are approvals, "
                "which are a separate question per action by design"
            )
        add("")
        add(
            "The first number is the staffing one. A repeatable check asked twenty "
            "times is one item in a queue; an approval is a separate decision each "
            "time, because two payments are two payments."
        )
    add("")

    # --- 4. what it would have got wrong ------------------------------------
    add("## 4. What the policy would have got wrong")
    add("")
    if r.reviewed is None:
        worksheet_note = (
            "The worksheet below lists every distinct refusal for you to rule on. "
            if r.worksheet
            else ""
        )
        add(
            "**Not reviewed.** This section cannot be computed from the audit: "
            "whether a refusal was right is a question about your work, not about "
            f"our records. {worksheet_note}Until that review happens, this report "
            "states no false-positive rate — not a rate of zero."
        )
    else:
        add(f"- Refusals reviewed: **{r.reviewed}**")
        add(f"- Ruled ordinary work: **{r.false_positives}**")
        if r.reviewed:
            rate = (r.false_positives or 0) * 100.0 / r.reviewed
            add(f"- False-positive rate over reviewed refusals: **{rate:.1f}%**")
    if not r.worksheet:
        add("")
        add(
            "No action in this window would have been refused outright, so there "
            "is nothing to rule on here. Section 3's questions are the ones that "
            "would have reached a person."
        )
    if r.worksheet:
        add("")
        add("| Would have refused | Resource / action | Count | Your verdict |")
        add("|---|---|---|---|")
        for line in r.worksheet:
            target = f"{line.resource}.{line.action}"
            add(f"| `{line.rule}` | {target} | {line.count} | |")
    add("")

    # --- 5. reach ------------------------------------------------------------
    add("## 5. What the agent could reach")
    add("")
    s = r.scope
    if s.reads == 0:
        add("No action in this window had a scope rule to apply.")
    else:
        add(
            f"Your agent read **{s.rows_returned}** rows across **{s.reads}** "
            f"scoped reads. The scope rule in this policy would have shown it "
            f"**{s.rows_returned - s.rows_removed}**."
        )
        if s.widest is not None:
            action, removed = s.widest
            add(f"- Widest single read: `{action}`, {removed} rows narrowed away")
        if s.partial_reads:
            add(
                f"- Reads counted only up to the evaluation cap: "
                f"**{s.partial_reads}** (those lines are lower bounds, not totals)"
            )
        for reason, count in s.unmeasured:
            add(f"- Could not be counted ({reason}): **{count}**")
        add("")
        add(
            "The rows it read are not rows it misused. What was done with them is "
            "not in our records."
        )
    add("")

    # --- 6. the rails question ----------------------------------------------
    add("## 6. What your own systems already catch")
    add("")
    if r.exclusive is None:
        add(
            "**Not joined.** Answering this needs your systems' outcomes for the "
            "same actions, which were not made available for this window. Without "
            "it, every count in §2 is an upper bound on what this policy adds: "
            "some of those actions your own controls would have refused anyway, "
            "and this report cannot say how many."
        )
    else:
        add(
            f"Of the refusals in §2, **{r.exclusive}** were not also refused by "
            "your own systems. That is this policy's exclusive contribution in "
            "this window."
        )
    add("")

    # --- 7. disclosures -------------------------------------------------------
    add("## 7. Disclosures")
    add("")
    d = r.disclosures
    if d.kill_orders:
        add(
            f"- **The one lever was used.** {d.kill_orders} action(s) were halted "
            "by an operator's kill order. Advisory refuses nothing else, so this "
            "is the only place the deployment changed what your estate did."
        )
    else:
        add("- No kill order was used. Nothing in this window was refused by us.")
    if d.kill_unavailable:
        add(
            f"- **{d.kill_unavailable} action(s) were refused because the kill "
            "store was unreachable.** Nobody ordered these. The gateway cannot "
            "know whether an operator has pulled the cord while that store is "
            "down, and it fails closed there by design — so these are refusals "
            "your estate experienced and this report does not count as advisory."
        )
    add(
        "- Every record in this report carries `enforcement: advisory`. A figure "
        "spanning both modes is refused by the generator rather than averaged."
    )
    add("")

    # --- 8. the limit ----------------------------------------------------------
    add("## 8. What this measurement cannot tell you")
    add("")
    add(
        "These numbers describe what your agents attempted while nothing stopped "
        "them. Under enforcement each agent would have seen the gateway's answer "
        "and done something else next: retried differently, asked a person, or "
        "given up. So the counts above are the truth about this window, not a "
        "simulation of an enforced one, and the direction of that error is not "
        "knowable in advance — some of those attempts would never have been made "
        "twice, and some would have become five attempts each."
    )
    add("")
    add(
        "This window is also just this window: one month-end, one set of holidays, "
        "one payment run. And it measures the policy we wrote together, not the "
        "product in general."
    )
    add("")
    add("---")
    add("")
    add("## Appendix — where each number comes from")
    add("")
    add("| Figure | Audit query |")
    add("|---|---|")
    for figure, query in _QUERIES:
        add(f"| {figure} | `{query}` |")
    add("")
    add(
        "Every figure above is reproducible from your own audit export with these "
        "queries. A report you cannot check is a brochure."
    )
    add("")
    return "\n".join(out)


_QUERIES: tuple[tuple[str, str], ...] = (
    ("Actions observed", "count(*) where agent = <agent>"),
    ("Judged / not judged", "count(*) group by coverage"),
    ("Cause of unjudged", "coverage='unjudged' group by rule / advised.rule"),
    ("Would have refused", "coverage='judged' and advised.decision='deny'"),
    ("Would have asked a human", "coverage='judged' and advised.decision='hold'"),
    ("Distinct questions", "count(distinct advised.dedupeKey) over the holds above"),
    ("Batches refused whole", "distinct (correlationId, batchAdvice.failingIndex)"),
    ("Items refused", "sum(itemAdvice.wouldRefuse)"),
    ("Rows read / narrowed", "sum(scopeWouldRemove.returned / .removed) where measured"),
    ("Kill orders", "rule like 'kill:%'"),
    ("Kill store unreachable", "rule = 'kill-unavailable'"),
)
