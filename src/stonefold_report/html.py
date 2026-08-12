# SPDX-License-Identifier: Apache-2.0
"""The Advisory Report as a single self-contained HTML page.

This is the artifact a pilot customer actually receives, so it obeys mail-room
physics: one file, no scripts, no external fonts, images or stylesheets —
nothing that breaks when the file is opened offline, forwarded, archived, or
printed for a steering meeting. Charts are inline SVG with native ``<title>``
tooltips; every chart's numbers also appear on the marks or in a table, so
nothing is color-alone and nothing is lost on paper. The one interactive
element is ``<details>`` (the query appendix), which needs no script.

It is written for three readers at once. The sponsor gets a computed executive
summary and section 9; the operator gets the attention figures and a worksheet
that can be completed with a pen on the printed page; the auditor gets the
provenance block and the reproduction queries, so the file is self-sufficient
as evidence. Both renderers read the same
:class:`~stonefold_report.figures.Report`, so the HTML and Markdown editions
cannot disagree on a number.

Palette: the site's warm-basalt/bronze tokens, with the three verdict hues
re-stepped for chart adjacency and validated (lightness band, chroma floor,
CVD separation, normal-vision floor) rather than eyeballed. The unjudged
segment is a deliberate neutral with a hatch texture and a direct label — it is
the hole in the coverage, and it should read as one.
"""

from __future__ import annotations

from datetime import date, datetime
from html import escape

from stonefold_report.figures import Report, verdict_key
from stonefold_report.render import plural

# --- brand tokens (site css), chart hues validated for adjacency ----------
_BG = "#f6f2e9"
_SURFACE = "#fdfbf5"
_INK = "#1b1712"
_MUTED = "#5d5445"
_LINE = "#e4dccb"
_BAND = "#efe9db"
_BRONZE = "#8a5f0e"  # judged / magnitude
_NEUTRAL = "#9c8f75"  # the unjudged hole; always hatched + labeled
_DENY = "#983122"
_HOLD = "#d19a26"
_OK = "#0a8a6b"

# Plain words for the rule codes. The code stays beside them for the engineer;
# the gloss is for the reader deciding whether to enforce, who should not need
# the policy grammar to understand their own report.
_GLOSSES: tuple[tuple[str, str], ...] = (
    ("default-deny", "not permitted by the policy"),
    ("scope-denied", "target outside the actor's scope"),
    ("scope-lost", "target left the actor's scope before dispatch"),
    ("unknown-action", "action not declared in the registry"),
    ("unmapped-tool", "tool not covered by the interception mapping"),
    ("items-over-ceiling", "more items than the declared ceiling"),
    ("gate:valueLimit", "over the declared value limit"),
    ("gate:spendLimit", "over the declared spend limit"),
    ("gate:rate", "over the declared rate limit"),
    ("gate:quota", "over the declared quota"),
    ("gate:quantityCap", "over the declared quantity cap"),
    ("gate:requireApproval", "requires a named approver"),
    ("gate:dualAuthorization", "requires two distinct approvers"),
    ("gate:precondition", "a required pre-check did not pass"),
    ("gate:denylist", "destination is on the blocked list"),
    ("gate:allowlist", "destination is not on the allowed list"),
    ("gate:contentCheck", "content check did not pass"),
    ("gate:disclosure", "disclosure rules forbid this destination"),
    ("gate:requireMatch", "no matching obligation to pay against"),
)


def _gloss(rule: str) -> str | None:
    for prefix, text in _GLOSSES:
        if rule == prefix or rule.startswith(prefix):
            return text
    if rule.endswith("-unavailable"):
        return "a dependency the decision needed was unreachable"
    return None


_CSS = f"""
:root {{ color-scheme: light; }}
* {{ box-sizing: border-box; margin: 0; }}
body {{ background: {_BG}; color: {_INK}; font: 15px/1.55 'IBM Plex Sans',
       'Segoe UI', system-ui, sans-serif; padding: 2rem 1rem; }}
main {{ max-width: 52rem; margin: 0 auto; background: {_SURFACE};
        border: 1px solid {_LINE}; padding: 2.5rem 3rem; }}
header.letterhead {{ display: flex; justify-content: space-between; gap: 1rem;
        align-items: flex-end; border-bottom: 2px solid {_BRONZE};
        padding-bottom: 1rem; margin-bottom: 1.2rem; flex-wrap: wrap; }}
.wordmark {{ font-family: Cinzel, Georgia, 'Times New Roman', serif;
        font-size: 1.05rem; letter-spacing: .3em; text-transform: uppercase; }}
h1, h2 {{ font-family: Cinzel, Georgia, 'Times New Roman', serif;
          font-weight: 600; letter-spacing: .02em; }}
h1 {{ font-size: 1.5rem; margin-top: .2rem; }}
h2 {{ font-size: 1.08rem; margin: 2.2rem 0 .7rem; border-top: 1px solid {_LINE};
      padding-top: 1.4rem; break-after: avoid; }}
.meta {{ color: {_MUTED}; font-size: .84rem; text-align: right;
         line-height: 1.5; }}
.meta b {{ color: {_INK}; }}
p, li {{ max-width: 46rem; }}
p {{ margin: .55rem 0; }}
.summary {{ font-size: 1.02rem; margin: 1rem 0 .6rem; max-width: 46rem; }}
.summary b {{ font-variant-numeric: tabular-nums; }}
.frame {{ background: {_BAND}; border-left: 3px solid {_BRONZE};
          padding: .8rem 1rem; margin: 1rem 0; font-size: .92rem; }}
.tiles {{ display: flex; gap: .6rem; flex-wrap: wrap; margin: 1.4rem 0;
          break-inside: avoid; }}
.tile {{ flex: 1 1 10rem; border: 1px solid {_LINE}; border-top: 3px solid {_LINE};
         padding: .7rem .9rem; background: {_SURFACE}; }}
.tile.deny {{ border-top-color: {_DENY}; }}
.tile.hold {{ border-top-color: {_HOLD}; }}
.tile.bronze {{ border-top-color: {_BRONZE}; }}
.tile b {{ display: block; font-size: 1.7rem; line-height: 1.15;
           font-variant-numeric: tabular-nums; }}
.tile span {{ color: {_MUTED}; font-size: .8rem; }}
figure {{ margin: 1rem 0; break-inside: avoid; }}
figcaption {{ color: {_MUTED}; font-size: .82rem; margin-top: .3rem; }}
svg {{ display: block; width: 100%; height: auto; }}
table {{ border-collapse: collapse; width: 100%; margin: .8rem 0;
         font-size: .9rem; break-inside: avoid; }}
th, td {{ text-align: left; padding: .4rem .6rem; border-bottom: 1px solid {_LINE}; }}
th {{ color: {_MUTED}; font-weight: 600; font-size: .78rem;
      text-transform: uppercase; letter-spacing: .06em; }}
td.n, th.n {{ text-align: right; font-variant-numeric: tabular-nums; }}
td.tick, th.tick {{ text-align: center; width: 6.2rem; }}
.box {{ display: inline-block; width: .95em; height: .95em;
        border: 1.5px solid {_MUTED}; border-radius: 2px; vertical-align: -.12em; }}
code {{ font-family: 'IBM Plex Mono', Consolas, monospace; font-size: .88em;
        background: {_BAND}; padding: 0 .25em; }}
.gloss {{ color: {_MUTED}; font-size: .86em; }}
.absent {{ border: 1px dashed {_NEUTRAL}; color: {_MUTED}; padding: .8rem 1rem;
           margin: .8rem 0; }}
.legend {{ display: flex; gap: 1.1rem; flex-wrap: wrap; color: {_MUTED};
           font-size: .8rem; margin: .3rem 0; }}
.legend i {{ display: inline-block; width: .7em; height: .7em;
             margin-right: .35em; border-radius: 2px; }}
.chips {{ display: flex; gap: 1.4rem; flex-wrap: wrap; margin: .6rem 0;
          font-size: .92rem; }}
.chips i {{ display: inline-block; width: .65em; height: .65em;
            border-radius: 50%; margin-right: .4em; }}
ol.path {{ margin: .6rem 0 .6rem 1.2rem; }}
ol.path li {{ margin: .5rem 0; }}
details {{ margin: 1rem 0; }}
summary {{ cursor: pointer; color: {_MUTED}; font-size: .9rem; }}
footer {{ color: {_MUTED}; font-size: .8rem; margin-top: 2.2rem;
          border-top: 1px solid {_LINE}; padding-top: 1rem; line-height: 1.6; }}
@media print {{ body {{ background: {_SURFACE}; padding: 0; }}
                main {{ border: none; padding: 0; }}
                details {{ display: block; }}
                details > * {{ display: block; }} }}
"""

_HATCH = (
    f'<defs><pattern id="hatch" width="6" height="6" patternTransform="rotate(45)"'
    f' patternUnits="userSpaceOnUse"><rect width="6" height="6" fill="{_NEUTRAL}"'
    f' opacity="0.35"/><line x1="0" y1="0" x2="0" y2="6" stroke="{_NEUTRAL}"'
    f' stroke-width="2"/></pattern>'
    f'<pattern id="hatchHold" width="6" height="6" patternTransform="rotate(45)"'
    f' patternUnits="userSpaceOnUse"><rect width="6" height="6" fill="{_HOLD}"'
    f' opacity="0.3"/><line x1="0" y1="0" x2="0" y2="6" stroke="{_HOLD}"'
    f' stroke-width="2"/></pattern></defs>'
)


def _bar_path(x: float, y: float, w: float, h: float, r: float = 4.0) -> str:
    """A horizontal bar, square at the baseline and rounded at the data end."""
    r = min(r, w / 2, h / 2)
    return (
        f"M{x:.1f} {y:.1f} h{w - r:.1f} q{r:.1f} 0 {r:.1f} {r:.1f} "
        f"v{h - 2 * r:.1f} q0 {r:.1f} {-r:.1f} {r:.1f} h{-(w - r):.1f} Z"
    )


def _stacked_bar(
    segments: list[tuple[str, int, str, bool]], total: int, *, height: int = 36
) -> str:
    """One horizontal stacked bar: (label, value, fill, hatched) per segment.

    2px surface gaps between segments. Values are printed ON the segments that
    are wide enough to hold them — native tooltips die on paper, and this chart
    must survive a steering-meeting printout — and every value also appears in
    the surrounding text or table.
    """
    if total <= 0:
        return ""
    width = 720.0
    parts: list[str] = [_HATCH]
    x = 0.0
    drawn = [s for s in segments if s[1] > 0]
    for index, (label, value, fill_color, hatched) in enumerate(drawn):
        w = max(width * value / total - 2, 1.0)
        fill = (
            ("url(#hatchHold)" if fill_color == _HOLD else "url(#hatch)")
            if hatched
            else fill_color
        )
        last = index == len(drawn) - 1
        shape = (
            f'<path d="{_bar_path(x, 0, w, height)}" fill="{fill}"/>'
            if last
            else f'<rect x="{x:.1f}" y="0" width="{w:.1f}" height="{height}" fill="{fill}"/>'
        )
        share = value * 100.0 / total
        text = ""
        inline = f"{value} · {share:.0f}%"
        if w >= 110:
            ink = _INK if hatched else _SURFACE
            text = (
                f'<text x="{x + w / 2:.1f}" y="{height / 2 + 4.5:.1f}" '
                f'text-anchor="middle" font-size="13" fill="{ink}">'
                f"{escape(inline)}</text>"
            )
        elif w >= 34:
            text = (
                f'<text x="{x + w / 2:.1f}" y="{height / 2 + 4.5:.1f}" '
                f'text-anchor="middle" font-size="12" fill="{_INK}">{value}</text>'
            )
        parts.append(
            f"<g>{shape}<title>{escape(label)}: {value} ({share:.1f}%)</title></g>{text}"
        )
        x += w + 2
    svg = "".join(parts)
    return (
        f'<svg viewBox="0 0 {width:.0f} {height}" role="img" '
        f'aria-label="{escape("; ".join(f"{s[0]} {s[1]}" for s in drawn))}">{svg}</svg>'
    )


def _hbars(rows: list[tuple[str, int, str]], *, unit: str = "") -> str:
    """Labeled horizontal bars, one per row: (label, value, color). The value
    sits at the data end; the label is real text left of the bar."""
    if not rows:
        return ""
    top = max(value for _l, value, _c in rows)
    if top <= 0:
        return ""
    width, bar_h, gap, label_w = 720.0, 22, 10, 264.0
    span = width - label_w - 60
    parts: list[str] = []
    y = 0
    for label, value, color in rows:
        w = max(span * value / top, 2.0)
        parts.append(
            f'<text x="{label_w - 10:.0f}" y="{y + bar_h - 6}" text-anchor="end" '
            f'font-size="13" fill="{_INK}">{escape(label)}</text>'
            f'<g><path d="{_bar_path(label_w, y, w, bar_h)}" fill="{color}"/>'
            f"<title>{escape(label)}: {value}{unit}</title></g>"
            f'<text x="{label_w + w + 8:.1f}" y="{y + bar_h - 6}" font-size="13" '
            f'fill="{_MUTED}">{value}{unit}</text>'
        )
        y += bar_h + gap
    return (
        f'<svg viewBox="0 0 {width:.0f} {y - gap}" role="img">'
        + "".join(parts)
        + "</svg>"
    )


def _columns(days: list[tuple[str, int]]) -> str:
    """The activity strip: every day of the window, zeros drawn as empty slots
    and weekends tinted — the silence is information too."""
    if not days:
        return ""
    top = max(count for _d, count in days) or 1
    width, height, base = 720.0, 128, 96
    slot = width / len(days)
    bar_w = min(max(slot - 4, 3.0), 40.0)
    parts: list[str] = []
    for index, (day, count) in enumerate(days):
        x_slot = index * slot
        try:
            weekend = date.fromisoformat(day).weekday() >= 5
        except ValueError:
            weekend = False
        if weekend:
            parts.append(
                f'<rect x="{x_slot:.1f}" y="0" width="{slot:.1f}" height="{base}" '
                f'fill="{_BAND}"/>'
            )
        x = x_slot + (slot - bar_w) / 2
        if count > 0:
            h = max(base * count / top, 2.0)
            r = min(4.0, bar_w / 2, h / 2)
            d = (
                f"M{x:.1f} {base:.1f} v{-(h - r):.1f} q0 {-r:.1f} {r:.1f} {-r:.1f} "
                f"h{bar_w - 2 * r:.1f} q{r:.1f} 0 {r:.1f} {r:.1f} v{h - r:.1f} Z"
            )
            parts.append(
                f'<g><path d="{d}" fill="{_BRONZE}"/>'
                f"<title>{escape(day)}: {count}</title></g>"
            )
        else:
            parts.append(
                f'<g><rect x="{x:.1f}" y="{base - 2}" width="{bar_w:.1f}" '
                f'height="2" fill="{_LINE}"/><title>{escape(day)}: 0</title></g>'
            )
        if len(days) <= 16 or index % max(1, len(days) // 8) == 0:
            parts.append(
                f'<text x="{x + bar_w / 2:.1f}" y="{height - 8}" font-size="10" '
                f'text-anchor="middle" fill="{_MUTED}">{escape(day[5:])}</text>'
            )
    parts.append(
        f'<line x1="0" y1="{base}" x2="{width:.0f}" y2="{base}" '
        f'stroke="{_LINE}" stroke-width="1"/>'
    )
    return (
        f'<svg viewBox="0 0 {width:.0f} {height}" role="img" '
        f'aria-label="actions per day, weekends tinted">' + "".join(parts) + "</svg>"
    )


def _tile(value: str, label: str, accent: str = "") -> str:
    cls = f"tile {accent}".strip()
    return f'<div class="{cls}"><b>{escape(value)}</b><span>{escape(label)}</span></div>'


def _legend(entries: list[tuple[str, str]]) -> str:
    return '<div class="legend">' + "".join(
        f'<span><i style="background:{color}"></i>{escape(label)}</span>'
        for label, color in entries
    ) + "</div>"


def _rule_label(rule: str) -> str:
    """The chart-row label: plain words where a gloss exists, the code where
    none does. The code always appears in the table beside the chart."""
    return _gloss(rule) or rule


def _strong(md_text: str) -> str:
    """Convert the shared conversion-path lines' ``**bold**`` to real ``<b>``
    after escaping — section 9's lead phrases are what a skimming sponsor
    reads, and stripping them flattens the one action-oriented section."""
    out: list[str] = []
    for index, part in enumerate(escape(md_text).split("**")):
        out.append(part if index % 2 == 0 else f"<b>{part}</b>")
    return "".join(out)


def _summary(r: Report) -> str:
    """The executive summary: the report in computed sentences, every figure
    from the same object every chart reads. Leads the page — the disclaimer
    stays, but a sponsor should meet the findings before the caveats."""
    c, w, q = r.coverage, r.would_have, r.questions
    days = r.activity.days_with_traffic
    bits: list[str] = []
    bits.append(
        f"Across <b>{days}</b> {plural(days, 'day')} with traffic, the record "
        f"shows <b>{c.observed}</b> {plural(c.observed, 'action')} by this agent"
    )
    if c.ratio is not None:
        bits.append(f"<b>{c.ratio:.1f}%</b> could be judged by the policy")
    verdict_bits = []
    if w.refused:
        verdict_bits.append(f"refused <b>{w.refused}</b>")
    if q.total_holds:
        verdict_bits.append(
            f"asked a person <b>{q.total_holds}</b> "
            f"{plural(q.total_holds, 'time')} (<b>{q.total_questions}</b> "
            f"distinct {plural(q.total_questions, 'question')})"
        )
    if verdict_bits:
        bits.append("it would have " + " and ".join(verdict_bits))
    else:
        bits.append("it would have allowed everything it judged")
    sentence = "; ".join(bits) + "."
    if r.reviewed is None and (w.refused or q.total_holds):
        sentence += (
            " Nothing is recommended for enforcement until the review in "
            "§4 comes back."
        )
    return f'<p class="summary">{sentence}</p>'


def render_html(
    report: Report,
    *,
    prepared_for: str | None = None,
    generated_at: datetime | None = None,
) -> str:
    r = report
    c, w, q, s, o = r.coverage, r.would_have, r.questions, r.scope, r.outcomes
    out: list[str] = []
    add = out.append

    window = "no records"
    if r.disclosures.first is not None and r.disclosures.last is not None:
        window = (
            f"{r.disclosures.first.date().isoformat()} — "
            f"{r.disclosures.last.date().isoformat()}"
        )
    lifecycle = o.settled + o.failed + o.cancelled

    add("<!doctype html><html lang='en'><head><meta charset='utf-8'>")
    add(f"<title>Advisory Report — {escape(r.agent)}</title>")
    add("<meta name='viewport' content='width=device-width, initial-scale=1'>")
    add(f"<style>{_CSS}</style></head><body><main>")

    # --- letterhead ---------------------------------------------------------
    add('<header class="letterhead"><div>')
    add('<div class="wordmark">Stonefold</div>')
    add("<h1>Advisory Report</h1>")
    add("</div>")
    add('<div class="meta">')
    if prepared_for:
        add(f"Confidential — prepared for <b>{escape(prepared_for)}</b><br>")
    add(f"Agent <b>{escape(r.agent)}</b><br>Window <b>{escape(window)}</b><br>")
    add("Mode: advisory throughout</div></header>")

    add(_summary(r))
    add(
        '<div class="frame">Nothing in this window was prevented. The gateway '
        "decided every action and acted on none of them; every number below is "
        "what a policy <em>would</em> have done, not what it stopped.</div>"
    )

    # the headline row
    ratio = "n/a" if c.ratio is None else f"{c.ratio:.1f}%"
    add('<div class="tiles">')
    add(_tile(str(c.observed), "actions observed", "bronze"))
    add(_tile(ratio, "of them judgeable by policy", "bronze"))
    add(_tile(str(w.refused), "would have been refused", "deny"))
    add(_tile(str(q.total_questions), "distinct questions for a person", "hold"))
    add("</div>")

    # --- 1. coverage -------------------------------------------------------
    add("<h2>1 · What we could see</h2>")
    add(
        _stacked_bar(
            [
                ("judged", c.judged, _BRONZE, False),
                ("not judged", c.unjudged, _NEUTRAL, True),
            ],
            c.observed,
        )
    )
    add(_legend([("judged", _BRONZE), ("not judged (hatched)", _NEUTRAL)]))
    if c.by_cause:
        add("<table><tr><th>Why an action could not be judged</th>"
            '<th class="n">Actions</th></tr>')
        for cause, count in c.by_cause:
            add(f'<tr><td>{escape(cause)}</td><td class="n">{count}</td></tr>')
        add("</table>")
        add(
            "<p>Enforcement over the judged share is a control with exactly this "
            "hole in it — worth deciding about deliberately, not discovering "
            "later.</p>"
        )
    if r.activity.by_day:
        add("<figure>")
        add(_columns(list(r.activity.by_day)))
        caption = (
            f"Actions per day, empty days and tinted weekends included · "
            f"traffic on {r.activity.days_with_traffic} of "
            f"{len(r.activity.by_day)} {plural(len(r.activity.by_day), 'day')}"
        )
        if r.activity.busiest is not None:
            day, count = r.activity.busiest
            caption += f" · busiest {day} ({count})"
        add(f"<figcaption>{escape(caption)}</figcaption></figure>")

    # --- 2. the counterfactual --------------------------------------------
    add("<h2>2 · What the policy would have done</h2>")
    rows = [
        (
            _rule_label(line.rule),
            line.count,
            _DENY if line.decision == "deny" else _HOLD,
        )
        for line in w.by_rule
    ]
    add(_hbars(rows))
    if rows:
        add(_legend([("would refuse", _DENY), ("would ask a human", _HOLD)]))
        add("<table><tr><th>Rule</th><th>Meaning</th><th>Actions</th>"
            '<th class="n">Count</th></tr>')
        for line in w.by_rule:
            gloss = _gloss(line.rule) or f"would {line.decision}"
            actions = ", ".join(line.actions) or "—"
            add(
                f"<tr><td><code>{escape(line.rule)}</code></td>"
                f'<td class="gloss">{escape(gloss)}</td>'
                f"<td>{escape(actions)}</td>"
                f'<td class="n">{line.count}</td></tr>'
            )
        add("</table>")
    add(
        f"<p>Of <b>{c.judged}</b> judged actions: <b>{w.allowed}</b> would have "
        f"been allowed, <b>{w.refused}</b> refused, <b>{q.total_holds}</b> sent "
        "to a person."
        + (
            f" <b>{w.batches_refused}</b> "
            f"{plural(w.batches_refused, 'batch', 'batches')} would have been "
            "refused whole." if w.batches_refused else ""
        )
        + (
            f" <b>{w.item_calls}</b> multi-item "
            f"{plural(w.item_calls, 'call')} would have been partly refused, "
            f"covering <b>{w.item_refusals}</b> "
            f"{plural(w.item_refusals, 'item')}."
            if w.item_calls else ""
        )
        + "</p>"
    )
    if lifecycle:
        add('<div class="chips">')
        add(f'<span><i style="background:{_OK}"></i><b>{o.settled}</b> landed</span>')
        if o.failed:
            add(
                f'<span><i style="background:{_DENY}"></i><b>{o.failed}</b> '
                "failed in your systems</span>"
            )
        if o.cancelled:
            add(
                f'<span><i style="background:{_NEUTRAL}"></i><b>{o.cancelled}</b> '
                "cancelled before dispatch</span>"
            )
        add("</div>")
        add(
            "<p>These are the settle reports — what actually happened to the "
            "effects that went through. Only actions with an external effect "
            "report one, so reads and record changes do not appear in this "
            "count; and because nothing was enforced, effects the policy would "
            "have refused or held ran and settled with the rest"
            + (
                f" — and on <b>{o.moved_out_of_scope}</b> of them the target "
                "moved out of scope between decision and dispatch; enforcement "
                "would have stopped those at the last gate."
                if o.moved_out_of_scope else "."
            )
            + "</p>"
        )

    # --- 3. attention ------------------------------------------------------
    add("<h2>3 · What it would have cost in attention</h2>")
    if q.total_holds == 0:
        add("<p>No action in this window would have been sent to a human.</p>")
    else:
        add('<div class="tiles">')
        add(_tile(str(q.total_questions), "questions a person would face", "hold"))
        add(_tile(str(q.total_holds), "times they would have been asked"))
        add("</div>")
        if q.busiest is not None:
            name, count = q.busiest
            gloss = None
            if "(" in name:  # "Resource.action (CODE)" — gloss the code if known
                gloss = _gloss(name.rsplit("(", 1)[1].rstrip(")"))
            add(
                f"<p>The busiest question — <code>{escape(name)}</code>"
                + (f", {escape(gloss)}" if gloss else "")
                + f" — accounts for <b>{count}</b> of those asks on its own.</p>"
            )
        add(
            "<p>The first number is the staffing one. A repeatable check asked "
            "twenty times is one queue item; an approval is a separate decision "
            "each time, because two payments are two payments.</p>"
        )

    # --- 4. what it would have got wrong ------------------------------------
    add("<h2>4 · What the policy would have got wrong</h2>")
    if r.reviewed is None:
        add(
            '<div class="absent"><b>Not reviewed.</b> Whether a refusal was '
            "right is a question about your work, not our records. Until the "
            "worksheet below comes back, this report states no false-positive "
            "rate — not a rate of zero.</div>"
        )
    else:
        rate = (
            f"{(r.false_positives or 0) * 100.0 / r.reviewed:.1f}%"
            if r.reviewed else "n/a"
        )
        add(
            f"<p>Refusals reviewed: <b>{r.reviewed}</b> · ruled ordinary work: "
            f"<b>{r.false_positives}</b> · false-positive rate: <b>{rate}</b></p>"
        )
    if r.worksheet:
        add(
            "<p><b>How to complete this:</b> print this page and tick one box "
            "per line, or return the rulings in writing keyed as "
            f"<code>{escape(verdict_key(r.worksheet[0]))}</code>. "
            "“Ordinary work” means the policy would have stopped something "
            "that should have happened.</p>"
        )
        add("<table><tr><th>Would have refused</th><th>Meaning</th>"
            '<th class="n">Count</th><th class="tick">Ordinary work</th>'
            '<th class="tick">Correctly refused</th><th class="tick">Unsure</th></tr>')
        for line in r.worksheet:
            gloss = _gloss(line.rule) or ""
            target = f"{line.resource}.{line.action}"
            add(
                f"<tr><td><code>{escape(line.rule)}</code><br>"
                f'<span class="gloss">{escape(target)}</span></td>'
                f'<td class="gloss">{escape(gloss)}</td>'
                f'<td class="n">{line.count}</td>'
                '<td class="tick"><span class="box"></span></td>'
                '<td class="tick"><span class="box"></span></td>'
                '<td class="tick"><span class="box"></span></td></tr>'
            )
        add("</table>")
    else:
        add(
            "<p>No action in this window would have been refused outright; "
            "§3's questions are the ones that would have reached a person.</p>"
        )

    # --- 5. reach -----------------------------------------------------------
    add("<h2>5 · What the agent could reach</h2>")
    if s.reads == 0:
        add("<p>No action in this window had a scope rule to apply.</p>")
    else:
        add(
            "<p>Some rules judge not whether an action may run but how much a "
            "read returns — which rows of a table the agent gets to see. This "
            "section measures that narrowing: what the agent received, against "
            "what a scoped policy would have let it receive.</p>"
        )
        shown = s.rows_returned - s.rows_removed
        add(
            _stacked_bar(
                [
                    ("scope would have shown", shown, _BRONZE, False),
                    ("scope would have withheld", s.rows_removed, _HOLD, True),
                ],
                s.rows_returned,
            )
        )
        add(_legend([
            ("would have shown", _BRONZE),
            ("would have withheld (hatched)", _HOLD),
        ]))
        add(
            f"<p>Across <b>{s.reads}</b> scoped reads the agent received "
            f"<b>{s.rows_returned}</b> row-results; scope would have withheld "
            f"<b>{s.rows_removed}</b>. Row-results, not distinct rows — this "
            "report keeps no row values to tell them apart, deliberately."
        )
        if s.widest is not None:
            action, removed = s.widest
            add(
                f" The widest single read (<code>{escape(action)}</code>) alone "
                f"would have lost <b>{removed}</b>."
            )
        add("</p>")
        for reason, count in s.unmeasured:
            add(f"<p>Could not be counted ({escape(reason)}): <b>{count}</b></p>")
        if s.partial_reads:
            add(
                f"<p>Reads counted only up to the evaluation cap: "
                f"<b>{s.partial_reads}</b> — those lines are lower bounds, "
                "not totals.</p>"
            )
        add(
            "<p>The rows it read are not rows it misused. What was done with "
            "them is not in our records.</p>"
        )

    # --- 6. the rails question ----------------------------------------------
    add("<h2>6 · What your own systems already catch</h2>")
    if r.exclusive is None:
        add(
            '<div class="absent"><b>Not joined.</b> This needs your systems&#39; '
            "outcomes for the same actions. Without it, every count in §2 is an "
            "upper bound on what this policy adds — some of it your own "
            "controls would have refused anyway, and this report cannot say how "
            "much.</div>"
        )
    else:
        add(
            f"<p>Of the refusals in §2, <b>{r.exclusive}</b> were in runs none "
            "of your own systems refused anything in. Joined at run level, so "
            "this is the conservative end; per-action outcome data sharpens "
            "it.</p>"
        )

    # --- 7. disclosures ------------------------------------------------------
    add("<h2>7 · Disclosures</h2><ul>")
    d = r.disclosures
    if d.kill_orders:
        add(
            f"<li><b>The one lever was used:</b> {d.kill_orders} "
            f"{plural(d.kill_orders, 'action')} "
            "halted by an operator&#39;s kill order — the only thing an "
            "advisory deployment refuses.</li>"
        )
    else:
        add("<li>No kill order was used. Nothing here was refused by us.</li>")
    if d.kill_unavailable:
        add(
            f"<li><b>{d.kill_unavailable}</b> "
            f"{plural(d.kill_unavailable, 'action')} refused because the "
            "kill store was unreachable — nobody ordered these; the gateway "
            "fails closed when it cannot know whether the cord was pulled.</li>"
        )
    add(
        "<li>Every record behind this report carries "
        "<code>enforcement: advisory</code>; the generator refuses figures that "
        "span modes rather than averaging them.</li></ul>"
    )

    # --- 8. the limit ---------------------------------------------------------
    add("<h2>8 · What this measurement cannot tell you</h2>")
    add(
        "<p>These numbers describe what your agents attempted while nothing "
        "stopped them. Under enforcement each agent would have seen the "
        "gateway&#39;s answer and done something else next — retried "
        "differently, asked a person, given up. The counts are the truth about "
        "this window, not a simulation of an enforced one, and the direction "
        "of the difference is not knowable in advance.</p>"
        "<p>This window is also just this window — one month-end, one set of "
        "holidays — and it measures the policy we wrote together, not the "
        "product in general.</p>"
    )

    # --- 9. the decision ------------------------------------------------------
    add("<h2>9 · What we would turn on first</h2>")
    from stonefold_report.render import _QUERIES, _conversion_path

    add('<ol class="path">')
    for item in _conversion_path(r):
        add(f"<li>{_strong(item)}</li>")
    add("</ol>")

    # --- appendix: the page is self-sufficient evidence -----------------------
    add("<details><summary>Appendix — where each number comes from</summary>")
    add('<table><tr><th>Figure</th><th>Audit query</th></tr>')
    for figure, query in _QUERIES:
        add(f"<tr><td>{escape(figure)}</td><td><code>{escape(query)}</code></td></tr>")
    add("</table><p>Every figure is reproducible from your own audit export "
        "with these queries. A report you cannot check is a brochure.</p></details>")

    # --- provenance ------------------------------------------------------------
    add("<footer>")
    stamp = (
        generated_at.strftime("%Y-%m-%d %H:%M UTC")
        if generated_at is not None
        else None
    )
    add(
        "Produced from <b>"
        f"{c.observed}</b> decision {plural(c.observed, 'record')} and "
        f"<b>{lifecycle}</b> settlement {plural(lifecycle, 'record')}, "
        f"agent <code>{escape(r.agent)}</code>, window "
        f"{escape(window)}."
        + (f" Generated {escape(stamp)}." if stamp else "")
    )
    if prepared_for:
        add(f" Prepared for {escape(prepared_for)} — confidential.")
    add(
        " The Markdown edition of this report carries the same figures and "
        "accompanies this file."
    )
    add("</footer>")
    add("</main></body></html>")
    return "".join(out)
