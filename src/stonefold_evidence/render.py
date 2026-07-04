"""Render an evidence pack to Markdown (PDF-ready).

Each section is keyed to the control it evidences (docs/14). Every regulatory label
and date is printed with its ``[VERIFY]`` marker intact — the report never presents a
citation as confirmed.
"""

from __future__ import annotations

from datetime import datetime

from stonefold_core.models import AuditRecord

from stonefold_evidence.pack import ControlEvidence, EvidencePack

_HONESTY = (
    "This pack evidences the **acting surface** — what the agent could do, who allowed "
    "it, how a human stops it, and what the record proves. It is **not a compliance "
    "claim**: the AI Act regulates the whole system (data governance, transparency, "
    "accuracy, risk management, conformity assessment [VERIFY]), most of which concerns "
    "the model, which the gateway does not touch. Whether a deployment is 'high-risk' is "
    "a question for counsel, not this repo (docs/14)."
)


def _ts(value: datetime | None) -> str:
    return value.isoformat(timespec="seconds") if value is not None else "—"


def _row(record: AuditRecord) -> str:
    ref = ".".join(p for p in (record.resource, record.action) if p) or "—"
    rule = (record.rule or "—").replace("|", "/")
    return (f"| {_ts(record.timestamp)} | {record.actor} | {ref} | "
            f"{record.decision.value} | {rule} | {record.outcome} |")


def _sample_table(records: list[AuditRecord]) -> list[str]:
    if not records:
        return []
    return [
        "",
        "| time | actor | resource.action | decision | rule | outcome |",
        "|---|---|---|---|---|---|",
        *[_row(r) for r in records],
    ]


def _control_section(ev: ControlEvidence) -> list[str]:
    c = ev.control
    lines = [
        f"## {c.label}",
        f"*{c.regime}*",
        "",
        f"**What the regulation asks [VERIFY]:** {c.asks}",
        "",
        f"**Gateway mechanism:** {c.mechanism}",
        "",
        f"**Specified:** {c.where}  ·  **Evidence artifact:** {c.artifact}",
        "",
        "**Evidence in this log:**",
    ]
    lines.extend(f"- {fact}" for fact in ev.facts)
    if not ev.present:
        lines.append("")
        lines.append("> No positive events of this kind in the covered window "
                     "(the machinery is present; the log simply shows none here).")
    lines.extend(_sample_table(ev.sample))
    lines.append("")
    return lines


def render_markdown(pack: EvidencePack) -> str:
    """Render the pack as a Markdown report."""
    window = (f"{_ts(pack.window[0])} .. {_ts(pack.window[1])}"
              if pack.window is not None else "—")
    decisions = ", ".join(f"{v} {k}" for k, v in sorted(pack.decision_counts.items())) or "—"
    lines: list[str] = [
        "# Audit evidence pack",
        "",
        f"> {pack.caveat}",
        "",
        f"- **Generated:** {_ts(pack.generated_at)}",
        f"- **Policy (documented control):** {pack.policy_ref or '(supply --policy)'}",
        f"- **Records:** {pack.total_records}  ·  **Window:** {window}",
        f"- **Decisions:** {decisions}",
        "",
        "Each section below is keyed to the control it evidences (docs/14 mapping). "
        "Citations are unverified — every `[VERIFY]` marker is the author's to resolve.",
        "",
    ]
    for ev in pack.controls:
        lines.extend(_control_section(ev))
    lines.append("---")
    lines.append("")
    lines.append(_HONESTY)
    lines.append("")
    return "\n".join(lines)
