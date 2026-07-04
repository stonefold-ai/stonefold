"""The control mapping the evidence pack is organised by — the docs/14 rows, re-encoded
as data so each report section can be keyed to a control.

Every regulatory label, article number, paragraph, and date carries the ``[VERIFY]``
marker exactly as docs/14 does: the citations are **unverified** until the author checks
them against Regulation (EU) 2024/1689 ``[VERIFY]``. This exporter is an engineering
mapping, **not legal advice**, and deploying the gateway does not make a system compliant
(docs/14 honesty section). The markers are load-bearing — do not strip them.
"""

from __future__ import annotations

from dataclasses import dataclass

# Printed once at the top of every pack (docs/14 draft banner, verbatim intent).
CITATION_CAVEAT = (
    "DRAFT — citations unverified. Every article number, paragraph, and date is marked "
    "[VERIFY] and MUST be checked against the regulation text (Regulation (EU) 2024/1689 "
    "[VERIFY]) before this report is relied on. This is an engineering mapping, NOT legal "
    "advice; deploying the gateway does not make an AI system compliant (docs/14)."
)


@dataclass(frozen=True)
class Control:
    """One control row from docs/14. ``key`` links a control to the audit evidence
    the pack computes for it (see ``stonefold_evidence.pack``)."""

    id: str
    regime: str      # "EU AI Act" | "DORA (adjacent)"
    label: str       # the obligation, with its [VERIFY] marker(s)
    asks: str        # what the regulation asks — [VERIFY] preserved
    mechanism: str   # the gateway mechanism that answers it
    where: str       # where it is specified (RFC/design)
    artifact: str    # the evidence artifact this section presents
    key: str         # the evidence bucket in pack.py


# The docs/14 mapping table, row for row. Text mirrors docs/14; [VERIFY] markers kept.
CONTROLS: tuple[Control, ...] = (
    Control(
        id="art-12",
        regime="EU AI Act",
        label="Art. 12 — Record-keeping [VERIFY]",
        asks="the system technically allows automatic recording of events (logs) over its "
             "lifetime, sufficient for traceability of its functioning [VERIFY]",
        mechanism="transactional audit: every evaluated action — allowed, held, denied, "
                  "halted — writes one append-only record; for executed effects the audit "
                  "write shares the DB transaction with the effect (no effect without a "
                  "record, no record without an effect); resultRefs link records to "
                  "downstream reality",
        where="RFC §11; CS-006",
        artifact="the audit log itself: who asked, what was decided, which gate, who "
                 "approved, what executed",
        key="record_keeping",
    ),
    Control(
        id="art-14-intervene",
        regime="EU AI Act",
        label="Art. 14 — Human oversight [VERIFY 14(4)]",
        asks="the system can be effectively overseen by natural persons; oversight includes "
             "the ability to intervene or interrupt it through a 'stop' button or similar "
             "[VERIFY 14(4)]",
        mechanism="approval holds (requireApproval, dualAuthorization): consequential "
                  "actions pause, staged, until a named human releases them — the agent "
                  "cannot release its own; kill-switch with the no-race guarantee: the stop "
                  "halts every action not yet dispatched, checked inside the same "
                  "serialized transaction that dispatches",
        where="RFC §7.8–7.9, §9; design §8.4",
        artifact="the approvals inbox; the halt audit records",
        key="oversight_intervene",
    ),
    Control(
        id="art-14-capacity",
        regime="EU AI Act",
        label="Art. 14 — oversight capacity [VERIFY 14(4)]",
        asks="overseers can correctly interpret output, decide not to use it, override or "
             "disregard it [VERIFY 14(4)]",
        mechanism="deterministic decisions with recorded reasons: no model in the "
                  "enforcement path, so every verdict is reproducible and explainable; each "
                  "record carries its deciding rule and, at audit: full, its per-gate results",
        where="RFC §1, §7.14",
        artifact="per-decision gate results at audit: full",
        key="oversight_capacity",
    ),
    Control(
        id="art-26",
        regime="EU AI Act",
        label="Art. 26 — Deployer obligations [VERIFY]",
        asks="use the system per its instructions; assign competent human oversight; "
             "monitor operation; keep the automatically generated logs (>= 6 months "
             "[VERIFY 26(6)])",
        mechanism="the policy file is the documented control — a short, readable, versioned "
                  "artifact stating exactly what the agent may do, signed by the compliance "
                  "officer; the audit store is the log-retention target",
        where="RFC §1, §11",
        artifact="the policy file in version control + the retained audit log",
        key="deployer",
    ),
    Control(
        id="dora",
        regime="DORA (adjacent) [VERIFY applicability]",
        label="DORA — ICT risk / auditability of automated actions [VERIFY applicability]",
        asks="financial entities carry parallel obligations (ICT risk management, "
             "auditability of automated actions) under DORA and existing SOX/PSD2/EBA "
             "outsourcing duties [VERIFY applicability]",
        mechanism="the same transactional audit record and approval/halt evidence serve "
                  "these filings (docs/13 §1 lists them per buyer)",
        where="docs/14 (adjacent), docs/13 §1",
        artifact="the same audit + approval evidence as Art. 12/14",
        key="record_keeping",
    ),
)


def control_by_id(control_id: str) -> Control:
    for control in CONTROLS:
        if control.id == control_id:
            return control
    raise KeyError(control_id)
