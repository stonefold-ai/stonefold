"""Guided CLI walkthrough of the (simple) Accounts-Payable demo (``make demo``).

Self-contained: runs the *real* agent loop and the *real* gateway over the
unmodified ``payments-ops.stele.yaml`` with an in-memory ledger — no Docker, no key
required (defaults to the scripted fake LLM; pass ``--provider anthropic|openai``,
or ``--provider auto`` with a key, to drive a real model). Output is ASCII only
(the Windows console is cp1252). Exits non-zero if any check fails, so it doubles
as a smoke test.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from stonefold_ap_demo.gateway import build_inmemory_bundle
from stonefold_ap_demo.llm import select_provider
from stonefold_ap_demo.scenarios import (
    approve_and_settle,
    scenario_approval,
    scenario_happy,
    scenario_process_inbox,
)

DEMO_NOW = datetime(2026, 6, 28, 12, 0, 0, tzinfo=timezone.utc)
_FIXED_CLOCK = lambda: DEMO_NOW  # noqa: E731 (deterministic demo clock)

OK = "[ OK ]"
BAD = "[FAIL]"
_failures: list[str] = []


def _hr(title: str) -> None:
    print("\n" + "=" * 72)
    print("  " + title)
    print("=" * 72)


def _check(ok: bool, message: str) -> None:
    print(f"  {OK if ok else BAD} {message}")
    if not ok:
        _failures.append(message)


def _decisions(result: object) -> str:
    out = []
    for d in getattr(result, "decisions", []):
        tag = str(d.get("decision", "?")).upper()
        out.append(f"      - {tag:7} {d.get('rule','')}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stonefold Accounts-Payable demo walkthrough")
    parser.add_argument("--provider", default="fake",
                        help="fake | auto | anthropic | openai (default: fake)")
    args = parser.parse_args(argv)
    provider = select_provider(args.provider)

    print("Stonefold Accounts-Payable Gateway - guided walkthrough")
    print("  policy : examples/payments-ops.stele.yaml (unmodified)")
    print(f"  agent  : {provider.label}")
    print("  ledger : in-memory fake bank (no real money; all data fictional)")

    # --- Beat 1: happy path ------------------------------------------------ #
    _hr("Beat 1 - happy path: pay the approved Acme invoice ($800)")
    r = scenario_happy(build_inmemory_bundle(clock=_FIXED_CLOCK), provider)
    print(_decisions(r))
    _check(any(p["amount"] == 800.0 for p in r.payments), "the $800 invoice was paid")

    # --- Beat 2: process the whole inbox ----------------------------------- #
    _hr("Beat 2 - process the inbox: allow / hold / deny, all three outcomes")
    inbox_bundle = build_inmemory_bundle(clock=_FIXED_CLOCK)
    inb = scenario_process_inbox(inbox_bundle, provider)
    print(_decisions(inb))
    decs = {d["decision"] for d in inb.decisions}
    _check(any(p["amount"] == 800.0 for p in inb.payments), "the $800 invoice was paid (allow)")
    _check("hold" in decs, "the $6,000 invoice was HELD for approval")
    _check("deny" in decs, "the sanctioned-country invoice was REFUSED by the gateway (deny)")

    # --- Beat 3: approval -------------------------------------------------- #
    _hr("Beat 3 - approval in the loop: pay Globex ($6,000)")
    ap_bundle = build_inmemory_bundle(clock=_FIXED_CLOCK)
    ap = scenario_approval(ap_bundle, provider)
    print(_decisions(ap))
    pending = ap.extra["pending"]
    _check(len(pending) == 1, "the mid-size payment is HELD for approval")
    if pending:
        settled = approve_and_settle(ap_bundle, pending[0].id)
        _check(settled == 1, "a payments-manager approves -> it proceeds")

    # --- Beat 4: audit ----------------------------------------------------- #
    _hr("Beat 4 - audit: every outcome is an append-only record")
    records = inbox_bundle.audit_reader.by_correlation("inbox")
    kinds = {r.decision.value for r in records}
    print(f"      inbox run recorded: {sorted(kinds)}")
    _check({"allow", "hold", "deny"} <= kinds, "allow, hold AND deny are all audited")

    # --- verdict ----------------------------------------------------------- #
    _hr("Summary")
    if _failures:
        print(f"  {BAD} {len(_failures)} check(s) failed:")
        for f in _failures:
            print(f"        - {f}")
        return 1
    print(f"  {OK} the gateway processed the inbox: small payment allowed,")
    print("         mid-size held for approval, sanctioned-country payment refused, all audited.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
