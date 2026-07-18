# SPDX-License-Identifier: Apache-2.0
"""stonefold_tck — the Stonefold conformance test kit (docs/12).

An implementation-independent, black-box test suite any gateway runs to prove
conformance to the Stele RFC (docs/01). The implementer writes ONE adapter — a
``ConformanceDriver`` — and runs ``run_conformance`` over it; the report states
which conformance profiles (``stonefold_tck.checks.ALL_PROFILES``, docs/12 §4)
the implementation certifies.

The kit core imports nothing from the reference gateway; the reference's own
adapter lives in ``stonefold_tck.adapters.reference`` as the worked example (and as
implementation #1).
"""

from stonefold_tck.checks import ALL_PROFILES, ConformanceFailure
from stonefold_tck.driver import (
    ALL_CAPABILITIES,
    AuditEntry,
    ConformanceDriver,
    LoadResult,
    Operation,
    SubmitResult,
    TckActor,
)
from stonefold_tck.runner import CheckResult, ConformanceReport, run_conformance

__all__ = [
    "ALL_CAPABILITIES",
    "ALL_PROFILES",
    "AuditEntry",
    "CheckResult",
    "ConformanceDriver",
    "ConformanceFailure",
    "ConformanceReport",
    "LoadResult",
    "Operation",
    "SubmitResult",
    "TckActor",
    "run_conformance",
]
