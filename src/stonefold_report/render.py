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

from datetime import datetime

from stonefold_report.figures import Report

_NO_PREVENTION = (
    "Nothing in this window was prevented. The gateway decided every action and "
    "acted on none of them, so every number below describes what a policy *would* "
    "have done, not what it stopped."
)


def _pct(value: float | None) -> str:
    return "not computable" if value is None else f"{value:.1f}%"


def _summary_md(r: Report) -> str:
    """The executive summary, computed — the same sentences the HTML edition
    leads with, from the same figures."""
    c, w, q = r.coverage, r.would_have, r.questions
    bits = [
        f"Over **{r.activity.days_with_traffic}** day(s) with traffic we "
        f"observed **{c.observed}** action(s) by this agent"
    ]
    if c.ratio is not None:
        bits.append(f"**{c.ratio:.1f}%** could be judged by the policy")
    verdicts = []
    if w.refused:
        verdicts.append(f"refused **{w.refused}**")
    if q.total_holds:
        verdicts.append(f"asked a person **{q.total_questions}** question(s)")
    bits.append(
        "it would have " + " and ".join(verdicts)
        if verdicts
        else "it would have allowed everything it judged"
    )
    text = "; ".join(bits) + "."
    if r.reviewed is None and (w.refused or q.total_holds):
        text += (
            " Nothing is recommended for enforcement until the review in §4 "
            "comes back."
        )
    return text


def render(
    report: Report,
    *,
    prepared_for: str | None = None,
    generated_at: datetime | None = None,
) -> str:
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
    line = f"*Window: {window}. Mode: advisory throughout."
    if prepared_for:
        line += f" Confidential — prepared for {prepared_for}."
    add(line + "*")
    add("")
    add(_summary_md(r))
    add("")
    add(_NO_PREVENTION)
    add("")

    # --- 1. coverage, first -----------------------------------------------
    add("## 1. What we could see")
    add("")
    c = r.coverage
    add(f"- Actions observed: **{c.observed}**")
    act = r.activity
    if act.busiest is not None:
        day, count = act.busiest
        add(
            f"- Traffic on **{act.days_with_traffic}** day(s); busiest {day} "
            f"with {count} actions"
        )
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
    o = r.outcomes
    if o.settled or o.failed or o.cancelled:
        add("")
        add(
            f"What actually happened to the effects that went through: "
            f"**{o.settled}** landed, **{o.failed}** failed in your systems, "
            f"**{o.cancelled}** were cancelled before dispatch."
        )
        if o.moved_out_of_scope:
            add(
                f"On **{o.moved_out_of_scope}** of them the target had moved out "
                "of scope between the decision and the dispatch — enforcement "
                "would have stopped those at the last gate, and that near-miss "
                "window is invisible to any control that checks only once."
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
        from stonefold_report.figures import verdict_key

        add("")
        add(
            "How to complete this: return one ruling per line, keyed "
            f"`{verdict_key(r.worksheet[0])}`-style, with the values "
            "`legitimate` (ordinary work the policy would have stopped), "
            "`correct`, or `unsure`."
        )
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
        if s.widest is not None:
            action, removed = s.widest
            add(
                f"The widest single read (`{action}`) returned rows the scope "
                f"rule would have withheld **{removed}** of."
            )
        add(
            f"Across all **{s.reads}** scoped reads the agent received "
            f"**{s.rows_returned}** row-results, of which scope would have "
            f"withheld **{s.rows_removed}**. (Row-results, not distinct rows: "
            "the same row read twice counts twice, and this report keeps no row "
            "values to tell them apart — that is deliberate.)"
        )
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
            f"Of the refusals in §2, **{r.exclusive}** were in runs none of your "
            "own systems refused anything in. Joined at run level — one of your "
            "refusals clears the whole run it happened in — so this is the "
            "conservative end of the exclusive contribution. Per-action outcome "
            "data sharpens it."
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
    # --- 9. the decision this report exists to inform -----------------------
    add("## 9. What we would turn on first")
    add("")
    for line in _conversion_path(r):
        add(line)
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
    lifecycle = r.outcomes.settled + r.outcomes.failed + r.outcomes.cancelled
    provenance = (
        f"*Produced from {r.coverage.observed} decision record(s) and "
        f"{lifecycle} settlement record(s), agent `{r.agent}`, window {window}."
    )
    if generated_at is not None:
        provenance += f" Generated {generated_at.strftime('%Y-%m-%d %H:%M UTC')}."
    add(provenance + "*")
    add("")
    return "\n".join(out)


def _conversion_path(r: Report) -> list[str]:
    """The ranking §9 promises: what to enforce, tune, or leave, by evidence.

    Deliberately conservative. Nothing is recommended for enforcement without a
    completed review — a rule with catches and no verdicts is a rule whose
    false positives simply have not been looked for yet — and coverage work is
    always named, because enforcing 94% of a path is a control with a hole in
    it."""
    out: list[str] = []
    ruled = r.reviewed is not None
    refusing = [line for line in r.worksheet]
    if not refusing and r.questions.total_holds == 0 and not r.coverage.by_cause:
        out.append(
            "Nothing in this window produced evidence to act on: no rule would "
            "have refused or held anything, and everything the agents did was "
            "judgeable. Either the policy is not yet written for this traffic, "
            "or this was a quiet fortnight — a longer window answers which."
        )
        return out
    if refusing and not ruled:
        out.append(
            "**Nothing is ready to enforce yet — not because the rules failed, "
            "but because §4's review has not happened.** A rule with catches and "
            "no verdicts is a rule whose false positives have not been looked "
            "for. Completing the worksheet is the single step that unlocks this "
            "section."
        )
    if ruled and refusing:
        out.append(
            "**Enforce first:** the reviewed rules with no refusal marked as "
            "ordinary work. Their behaviour under enforcement is exactly what "
            "§2 shows, minus nothing."
            if r.false_positives == 0
            else "**Tune before enforcing:** the review marked some refusals as "
            "ordinary work, so those rules would stop real work today. The "
            "dataset is complete and the decision is deterministic, so an "
            "amended rule can be re-run against this same window before "
            "anything is switched on."
        )
    if r.questions.total_holds:
        out.append(
            f"**The approval load is measured:** {r.questions.total_questions} "
            "question(s) over the window (§3). Whether the role that would "
            "answer them can absorb that is a staffing judgement this report "
            "informs but cannot make — the role is named in the policy beside "
            "this report."
        )
    if r.coverage.by_cause:
        causes = "; ".join(f"{c} ({n})" for c, n in r.coverage.by_cause)
        out.append(
            f"**Coverage work before or alongside any of it:** {causes}. "
            "Turning enforcement on beside an unjudged path moves the traffic, "
            "not the risk."
        )
    out.append(
        "Advisory continues to run through any of these steps — every change "
        "can be measured against live traffic the same way this report was."
    )
    return out


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
