# SPDX-License-Identifier: Apache-2.0
"""The Advisory Report generator (advisory profile, spec docs/01 §10a).

Two weeks of advisory mode produce an audit whose records carry what the policy
*would* have done. This turns that dataset into the document a pilot customer
reads: coverage first, then the counterfactual, then what it would have cost in
human attention, then what it would have got wrong.

Read-only over the audit, by construction — the package imports no store, no
connector and no pipeline, so a report can be produced from an exported log by
someone who cannot reach the estate at all.
"""

from stonefold_report.figures import (
    MixedDatasetError,
    NotAdvisoryError,
    Report,
    build_report,
)
from stonefold_report.render import render

__all__ = [
    "MixedDatasetError",
    "NotAdvisoryError",
    "Report",
    "build_report",
    "render",
]
