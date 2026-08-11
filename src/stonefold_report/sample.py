# SPDX-License-Identifier: Apache-2.0
"""Generate a sample Advisory Report over a synthetic accounts-payable estate.

    python -m stonefold_report.sample -o report.html
    python -m stonefold_report.sample -o report.md

Everything in the output is fictional: the estate is seeded in memory, the
agent is a loop in this file, and the fortnight of timestamps is synthesized.
The point is to show the exact document a real pilot produces — same
generator, same sections, same refusals — without implying any pilot happened.
The sample published on stonefold.ai is this script's output, unedited except
for a banner saying what it is.

The traffic is shaped like the accounts-payable estate the use cases describe:
routine matched payments, a couple of invoices over the value limit, one
unmatched invoice retried by an agent that will not take a hint, a supplier
bank-change request, and a few calls to a tool nothing declared.
"""

from __future__ import annotations

import argparse
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from stonefold_core import (
    Actor,
    Connectors,
    InMemoryAuditSink,
    RawCall,
    Session,
    enforce,
    load_policy,
)
from stonefold_core.enums import EnforcementMode
from stonefold_core.registry import load_registry
from stonefold_core.scope import make_scope_resolver
from stonefold_connectors import InMemoryConnector
from stonefold_gates.base import CheckResult, GateContext, check_hold, check_pass
from stonefold_gates.engine import DefaultGateEngine
from stonefold_store import InMemoryOutboxStore
from stonefold_store.dispatch import DispatchWorker
from stonefold_report import build_report, render, render_html
from stonefold_report.figures import Report

_REGISTRY = {
    "scopePredicates": ["assignedToCurrentUser"],
    "resources": {
        "Invoice": {"actions": {
            "read": {"kind": "observe", "data": {}},
            "pay": {"kind": "effect", "reversibility": "irreversible",
                    "data": {"id": {"type": "string", "required": True},
                             "amount": {"type": "number", "required": True},
                             "supplier": {"type": "string", "required": True}}},
        }},
        "Supplier": {"actions": {
            "updateBankAccount": {"kind": "record",
                "data": {"id": {"type": "string", "required": True},
                         "iban": {"type": "string", "required": True}}},
        }},
    },
    "preconditionChecks": [
        {"name": "matchedToOrder", "holdCapable": True,
         "reasonCodes": {"NO_MATCHING_PO": "escalate"}},
    ],
}

_POLICY = {
    "agent": "ap-desk",
    "allow": [
        {"observe": ["read"]},
        {"effect": ["pay"]},
        {"record": ["updateBankAccount"]},
    ],
    "scope": {"Invoice": "assignedToCurrentUser"},
    "gates": {
        "pay": {
            "valueLimit": {"field": "data.amount", "max": 5000},
            "precondition": {"checks": ["matchedToOrder"],
                             "resolvers": "role:ap-clerk"},
        },
        "Supplier.updateBankAccount": {
            "requireApproval": {"approvers": "role:controller"}
        },
    },
    "defaults": {"enforcement": "advisory"},
}

_UNMATCHED = {"INV-77", "INV-31"}


def _matched_to_order(gctx: GateContext) -> CheckResult:
    invoice = str(gctx.resolved.data.get("id"))
    if invoice in _UNMATCHED:
        return check_hold("NO_MATCHING_PO", {"invoice": invoice})
    return check_pass()


def build_sample_report(seed: int = 11) -> Report:
    """Drive the synthetic fortnight and return its figures."""
    registry = load_registry(_REGISTRY)
    policy = load_policy(_POLICY, registry, schema=None, advisory_permitted=True)
    audit = InMemoryAuditSink()
    outbox = InMemoryOutboxStore(audit=audit)
    ledger = InMemoryConnector({
        "Invoice": [
            {"id": f"INV-{i}", "owner_id": "ap-bot" if i <= 80 else "other-desk"}
            for i in range(1, 121)
        ],
        "Supplier": [{"id": "SUP-1"}],
    })
    connectors = Connectors({"in_memory": ledger, "sql": ledger, "email": ledger})
    engine = DefaultGateEngine(
        registry, preconditions={"matchedToOrder": _matched_to_order}
    )
    scopes = make_scope_resolver(policy)

    def submit(call: RawCall, times: int = 1) -> None:
        for _ in range(times):
            enforce(
                call, Actor(id="ap-bot"), Session(id="s1", correlation_id="run-1"),
                registry=registry, audit=audit, policy=policy, gates=engine,
                scopes=scopes, outbox=outbox, connectors=connectors,
                enforcement=EnforcementMode.ADVISORY,
            )

    submit(RawCall(resource="Invoice", action="read", data={}), 14)
    for i in (3, 8, 12, 19, 24, 44, 51, 62, 70, 12, 24):  # routine matched pays
        submit(RawCall(resource="Invoice", action="pay",
                       data={"id": f"INV-{i}", "amount": 940.0 + i,
                             "supplier": "Meridian Supply"}))
    for i in (5, 9):  # over the value limit, retried
        submit(RawCall(resource="Invoice", action="pay",
                       data={"id": f"INV-{i}", "amount": 18400.0,
                             "supplier": "Northgate Works"}), 3)
    submit(RawCall(resource="Invoice", action="pay",  # unmatched, agent insists
                   data={"id": "INV-77", "amount": 2300.0,
                         "supplier": "Halewood Labs"}), 7)
    submit(RawCall(resource="Invoice", action="pay",
                   data={"id": "INV-31", "amount": 640.0,
                         "supplier": "Trellis Ltd"}), 2)
    submit(RawCall(resource="Supplier", action="updateBankAccount",  # crown jewel
                   data={"id": "SUP-1", "iban": "DE89 3704 0044 0532 0130 00"}), 2)
    submit(RawCall(resource="Vendor", action="onboard",  # nothing declared this
                   data={"name": "thin air"}), 3)
    DispatchWorker(outbox, connectors, registry=registry, scopes=scopes).drain()

    # A plausible fortnight: two working weeks, weekends silent, a burst on the
    # first Tuesday. Deterministic under the seed so the sample is reproducible.
    rng = random.Random(seed)
    start = datetime(2026, 7, 27, 9, 0, tzinfo=timezone.utc)
    weekdays = [d for d in range(12) if (start + timedelta(days=d)).weekday() < 5]
    stamped = [
        record.model_copy(update={
            "timestamp": start
            + timedelta(days=rng.choice(weekdays + [1, 1, 1]),
                        minutes=rng.randint(0, 480)),
        })
        for record in audit.all_records()
    ]
    return build_report(stamped, agent="ap-desk")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="stonefold_report.sample", description=__doc__
    )
    parser.add_argument("-o", "--out", type=Path, help="write here (suffix picks the form)")
    parser.add_argument("--seed", type=int, default=11)
    args = parser.parse_args(argv)

    report = build_sample_report(seed=args.seed)
    stamp = datetime(2026, 8, 11, tzinfo=timezone.utc)
    if args.out is not None and args.out.suffix == ".html":
        text = render_html(report, generated_at=stamp)
    else:
        text = render(report, generated_at=stamp)
    if args.out is not None:
        args.out.write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
