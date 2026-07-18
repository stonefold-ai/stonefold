# SPDX-License-Identifier: Apache-2.0
"""stonefold_evidence — the audit evidence-pack exporter (implementation-plan Workstream G3).

Turns the gateway's audit log into a human-readable report (Markdown, PDF-ready) whose
sections are keyed to the controls they evidence, reusing the docs/14 EU AI Act mapping
rows. The compliance buyer forwards it to their auditor: the product's own output becomes
their deliverable.

**Read-only.** Nothing here touches the enforcement path or writes to the audit store;
it only *reads* audit records. **The `[VERIFY]` markers on every regulatory label and
date are printed verbatim** — the citations in docs/14 are unverified until the author
checks them against the regulation text, so this exporter never presents a control
reference as confirmed (docs/14 header; opus-instructions constraint).
"""

from __future__ import annotations

from stonefold_evidence.controls import CONTROLS, Control
from stonefold_evidence.pack import ControlEvidence, EvidencePack, build_evidence_pack
from stonefold_evidence.render import render_markdown

__all__ = [
    "CONTROLS",
    "Control",
    "ControlEvidence",
    "EvidencePack",
    "build_evidence_pack",
    "render_markdown",
]
