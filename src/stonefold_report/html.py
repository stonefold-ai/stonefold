# SPDX-License-Identifier: Apache-2.0
"""The Advisory Report as a single self-contained HTML page.

This is the artifact a pilot customer actually receives, so it obeys mail-room
physics: one file, no scripts, no external fonts, images or stylesheets —
nothing that breaks when the file is opened offline, forwarded, archived, or
printed for a steering meeting. Charts are inline SVG with native ``<title>``
tooltips; every chart's numbers also appear as text or a table, so nothing is
color-alone and nothing is lost on paper.

Both renderers read the same :class:`~stonefold_report.figures.Report`, so the
HTML and the Markdown cannot disagree on a number. The prose here is terser —
the charts carry the weight — but every claim, refusal and absence rule is the
same: absent never zero, no prevention language, no row values.

Palette: the site's warm-basalt/bronze tokens, with the three verdict hues
re-stepped for chart adjacency and validated (lightness band, chroma floor,
CVD separation, normal-vision floor) rather than eyeballed. The unjudged
segment is a deliberate neutral with a hatch texture and a direct label — it is
the hole in the coverage, and it should read as one.
"""

from __future__ import annotations

from html import escape

from stonefold_report.figures import Report, verdict_key

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

_CSS = f"""
:root {{ color-scheme: light; }}
* {{ box-sizing: border-box; margin: 0; }}
body {{ background: {_BG}; color: {_INK}; font: 15px/1.55 'IBM Plex Sans',
       'Segoe UI', system-ui, sans-serif; padding: 2rem 1rem; }}
main {{ max-width: 52rem; margin: 0 auto; background: {_SURFACE};
        border: 1px solid {_LINE}; padding: 2.5rem 3rem; }}
h1, h2 {{ font-family: Cinzel, Georgia, 'Times New Roman', serif;
          font-weight: 600; letter-spacing: .02em; }}
h1 {{ font-size: 1.55rem; }}
h2 {{ font-size: 1.1rem; margin: 2.2rem 0 .7rem; border-top: 1px solid {_LINE};
      padding-top: 1.4rem; }}
p, li {{ max-width: 46rem; }}
p {{ margin: .55rem 0; }}
.kicker {{ color: {_MUTED}; font-size: .85rem; text-transform: uppercase;
           letter-spacing: .14em; }}
.window {{ color: {_MUTED}; font-size: .9rem; margin-top: .25rem; }}
.frame {{ background: {_BAND}; border-left: 3px solid {_BRONZE};
          padding: .8rem 1rem; margin: 1rem 0; font-size: .95rem; }}
.tiles {{ display: flex; gap: .6rem; flex-wrap: wrap; margin: 1.4rem 0; }}
.tile {{ flex: 1 1 10rem; border: 1px solid {_LINE}; padding: .7rem .9rem;
         background: {_SURFACE}; }}
.tile b {{ display: block; font-size: 1.7rem; line-height: 1.15;
           font-variant-numeric: tabular-nums; }}
.tile span {{ color: {_MUTED}; font-size: .8rem; }}
figure {{ margin: 1rem 0; }}
figcaption {{ color: {_MUTED}; font-size: .82rem; margin-top: .3rem; }}
svg {{ display: block; width: 100%; height: auto; }}
table {{ border-collapse: collapse; width: 100%; margin: .8rem 0;
         font-size: .9rem; }}
th, td {{ text-align: left; padding: .4rem .6rem; border-bottom: 1px solid {_LINE}; }}
th {{ color: {_MUTED}; font-weight: 600; font-size: .8rem;
      text-transform: uppercase; letter-spacing: .06em; }}
td.n, th.n {{ text-align: right; font-variant-numeric: tabular-nums; }}
code {{ font-family: 'IBM Plex Mono', Consolas, monospace; font-size: .88em;
        background: {_BAND}; padding: 0 .25em; }}
.absent {{ border: 1px dashed {_NEUTRAL}; color: {_MUTED}; padding: .8rem 1rem;
           margin: .8rem 0; }}
.legend {{ display: flex; gap: 1.1rem; flex-wrap: wrap; color: {_MUTED};
           font-size: .8rem; margin: .3rem 0; }}
.legend i {{ display: inline-block; width: .7em; height: .7em;
             margin-right: .35em; border-radius: 2px; }}
footer {{ color: {_MUTED}; font-size: .8rem; margin-top: 2.2rem;
          border-top: 1px solid {_LINE}; padding-top: 1rem; }}
@media print {{ body {{ background: {_SURFACE}; padding: 0; }}
                main {{ border: none; padding: 0; }} }}
"""

_HATCH = (
    f'<defs><pattern id="hatch" width="6" height="6" patternTransform="rotate(45)"'
    f' patternUnits="userSpaceOnUse"><rect width="6" height="6" fill="{_NEUTRAL}"'
    f' opacity="0.35"/><line x1="0" y1="0" x2="0" y2="6" stroke="{_NEUTRAL}"'
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
    segments: list[tuple[str, int, str, bool]], total: int, *, height: int = 34
) -> str:
    """One horizontal stacked bar: (label, value, color, hatched) per segment,
    2px surface gaps, direct labels underneath (never color alone)."""
    if total <= 0:
        return ""
    width = 720.0
    parts: list[str] = [_HATCH]
    x = 0.0
    drawn = [s for s in segments if s[1] > 0]
    for index, (label, value, color, hatched) in enumerate(drawn):
        w = max(width * value / total - 2, 1.0)
        fill = "url(#hatch)" if hatched else color
        last = index == len(drawn) - 1
        shape = (
            f'<path d="{_bar_path(x, 0, w, height)}" fill="{fill}"/>'
            if last
            else f'<rect x="{x:.1f}" y="0" width="{w:.1f}" height="{height}" fill="{fill}"/>'
        )
        parts.append(f"<g>{shape}<title>{escape(label)}: {value}</title></g>")
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
    width, bar_h, gap, label_w = 720.0, 22, 10, 250.0
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
    """A small per-day column strip — when the work happened."""
    if not days:
        return ""
    top = max(count for _d, count in days) or 1
    width, height, base = 720.0, 120, 96
    slot = width / len(days)
    bar_w = min(max(slot - 4, 3.0), 40.0)
    parts: list[str] = []
    for index, (day, count) in enumerate(days):
        h = max(base * count / top, 2.0)
        x = index * slot + (slot - bar_w) / 2
        # columns: square base, rounded top — the vertical twin of _bar_path
        r = min(4.0, bar_w / 2, h / 2)
        d = (
            f"M{x:.1f} {base:.1f} v{-(h - r):.1f} q0 {-r:.1f} {r:.1f} {-r:.1f} "
            f"h{bar_w - 2 * r:.1f} q{r:.1f} 0 {r:.1f} {r:.1f} v{h - r:.1f} Z"
        )
        parts.append(
            f'<g><path d="{d}" fill="{_BRONZE}"/><title>{escape(day)}: {count}</title></g>'
        )
        if len(days) <= 16 or index % max(1, len(days) // 8) == 0:
            parts.append(
                f'<text x="{x + bar_w / 2:.1f}" y="{height - 6}" font-size="10" '
                f'text-anchor="middle" fill="{_MUTED}">{escape(day[5:])}</text>'
            )
    return (
        f'<svg viewBox="0 0 {width:.0f} {height}" role="img" '
        f'aria-label="actions per day">' + "".join(parts) + "</svg>"
    )


def _tile(value: str, label: str) -> str:
    return f'<div class="tile"><b>{escape(value)}</b><span>{escape(label)}</span></div>'


def _legend(entries: list[tuple[str, str]]) -> str:
    return '<div class="legend">' + "".join(
        f'<span><i style="background:{color}"></i>{escape(label)}</span>'
        for label, color in entries
    ) + "</div>"


def render_html(report: Report) -> str:
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

    add(f"<!doctype html><html lang='en'><head><meta charset='utf-8'>")
    add(f"<title>Advisory Report — {escape(r.agent)}</title>")
    add(f"<meta name='viewport' content='width=device-width, initial-scale=1'>")
    add(f"<style>{_CSS}</style></head><body><main>")
    add('<div class="kicker">Stonefold — Advisory Report</div>')
    add(f"<h1>Agent <code>{escape(r.agent)}</code></h1>")
    add(f'<div class="window">Window {escape(window)} · advisory throughout</div>')
    add(
        '<div class="frame">Nothing in this window was prevented. The gateway '
        "decided every action and acted on none of them; every number below is "
        "what a policy <em>would</em> have done, not what it stopped.</div>"
    )

    # the headline row
    ratio = "n/a" if c.ratio is None else f"{c.ratio:.1f}%"
    add('<div class="tiles">')
    add(_tile(str(c.observed), "actions observed"))
    add(_tile(ratio, "of them judgeable by policy"))
    add(_tile(str(w.refused), "would have been refused"))
    add(_tile(str(q.total_questions), "questions for a person"))
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
    if r.activity.busiest is not None:
        add(_columns(list(r.activity.by_day)))
        day, count = r.activity.busiest
        add(
            f'<figure><figcaption>Actions per day · traffic on '
            f"{r.activity.days_with_traffic} day(s), busiest {escape(day)} "
            f"({count} actions)</figcaption></figure>"
        )

    # --- 2. the counterfactual --------------------------------------------
    add("<h2>2 · What the policy would have done</h2>")
    rows = [
        (
            f"{line.rule} · would {line.decision}",
            line.count,
            _DENY if line.decision == "deny" else _HOLD,
        )
        for line in w.by_rule
    ]
    add(_hbars(rows))
    if rows:
        add(_legend([("would refuse", _DENY), ("would ask a human", _HOLD)]))
    add(
        f"<p>Of <b>{c.judged}</b> judged actions: <b>{w.allowed}</b> would have "
        f"been allowed, <b>{w.refused}</b> refused, <b>{q.total_holds}</b> sent "
        "to a person."
        + (
            f" <b>{w.batches_refused}</b> batch(es) would have been refused "
            "whole." if w.batches_refused else ""
        )
        + (
            f" <b>{w.item_calls}</b> multi-item call(s) would have been partly "
            f"refused, covering <b>{w.item_refusals}</b> items."
            if w.item_calls else ""
        )
        + "</p>"
    )
    if o.settled or o.failed or o.cancelled:
        add(
            f"<p>What actually happened to the effects that went through: "
            f"<b>{o.settled}</b> landed, <b>{o.failed}</b> failed in your own "
            f"systems, <b>{o.cancelled}</b> were cancelled before dispatch."
            + (
                f" On <b>{o.moved_out_of_scope}</b> the target moved out of "
                "scope between decision and dispatch — enforcement would have "
                "stopped those at the last gate."
                if o.moved_out_of_scope else ""
            )
            + "</p>"
        )

    # --- 3. attention ------------------------------------------------------
    add("<h2>3 · What it would have cost in attention</h2>")
    if q.total_holds == 0:
        add("<p>No action in this window would have been sent to a human.</p>")
    else:
        add('<div class="tiles">')
        add(_tile(str(q.total_questions), "questions a person would face"))
        add(_tile(str(q.total_holds), "times they would have been asked"))
        if q.busiest is not None:
            add(_tile(str(q.busiest[1]), f"× the busiest: {q.busiest[0]}"))
        add("</div>")
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
        add("<table><tr><th>Would have refused</th><th>Action</th>"
            '<th class="n">Count</th><th>Your verdict</th></tr>')
        for line in r.worksheet:
            add(
                f"<tr><td><code>{escape(line.rule)}</code></td>"
                f"<td>{escape(str(line.resource))}.{escape(str(line.action))}</td>"
                f'<td class="n">{line.count}</td><td></td></tr>'
            )
        add("</table>")
        add(
            f"<p>Rulings are keyed <code>{escape(verdict_key(r.worksheet[0]))}"
            "</code>-style, one per rule-and-action.</p>"
        )
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
                f"<p><b>{s.partial_reads}</b> read(s) were counted only up to "
                "the evaluation cap — those lines are lower bounds.</p>"
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
            f"<li><b>The one lever was used:</b> {d.kill_orders} action(s) "
            "halted by an operator&#39;s kill order — the only thing an "
            "advisory deployment refuses.</li>"
        )
    else:
        add("<li>No kill order was used. Nothing here was refused by us.</li>")
    if d.kill_unavailable:
        add(
            f"<li><b>{d.kill_unavailable}</b> action(s) refused because the "
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
    add("<h2>9 · What we would turn on first</h2><ul>")
    from stonefold_report.render import _conversion_path

    for item in _conversion_path(r):
        text = item.replace("**", "")
        add(f"<li>{escape(text)}</li>")
    add("</ul>")

    add(
        "<footer>Every figure is reproducible from your own audit export — the "
        "queries are listed in the Markdown edition of this report, which "
        "accompanies it. A report you cannot check is a brochure.</footer>"
    )
    add("</main></body></html>")
    return "".join(out)
