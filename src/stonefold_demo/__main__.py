# SPDX-License-Identifier: Apache-2.0
"""The adversarial demo (M-DEMO). Run with ``python -m stonefold_demo`` (or ``make demo``).

Makes the product's core claim visible and attackable: the *same* prompt-injected
agent run, once with no gateway (the data leaves) and once through the Stonefold gateway
(the exfiltration is refused and the audit proves it); then an operator kills a
live run; then an open invitation to make the gated agent leak - which it can't.

Nothing here is asserted by faith: the script re-checks the key invariants and
exits non-zero if anything escapes, so ``make demo`` doubles as a smoke test.
"""

from __future__ import annotations

from stonefold_core import Decision
from stonefold_demo.agent import INJECTED_TICKET
from stonefold_demo.scenarios import (
    G1Result,
    G2Result,
    G3Result,
    g1_injection_blocked,
    g2_live_kill,
    g3_invite_attack,
)

# ASCII-only output: this runs in whatever console the viewer has (Windows cp1252
# included), so we avoid Unicode glyphs that can't encode there.
_BAR = "=" * 72
_OK = "  [OK] "
_NO = "  [!!] "
_ARROW = " -> "


def _h(title: str) -> None:
    print(f"\n{_BAR}\n  {title}\n{_BAR}")


def act1(r: G1Result) -> bool:
    _h("ACT 1 - a prompt-injection, blocked end to end  (G1)")
    print("The agent is asked to handle this support ticket:\n")
    for line in INJECTED_TICKET.splitlines():
        print(f"    | {line}")
    print("\nThe hidden instruction tries to exfiltrate every customer's PII.\n")

    print("RUN A - no gateway (the agent's tools fire directly):")
    print(f"{_NO}read returned ALL {r.unprotected.customers_seen} customers (no scope)")
    print(f"{_NO}emailed the data to: {', '.join(r.unprotected.external_emails)}")
    print(f"{_NO}bulk export ran ({r.unprotected.exports} dump). DATA LEFT THE BUILDING.\n")

    print("RUN B - through the Stonefold gateway (same intents):")
    print(f"{_OK}read was scoped below the model: {r.scoped_read_rows} rows (only alice's own)")
    for step in r.gated.steps:
        mark = _OK if step.decision in (Decision.DENY, Decision.HALT) or not step.intent.malicious else _NO
        verdict = step.decision.value.upper()
        print(f"{mark}{step.intent.label}{_ARROW}{verdict}  [{step.rule}]")
    sent = r.gated_world.emails_sent_to()
    print(f"\n  Emails that actually left the gateway: {sent or 'none'}")
    print(f"  Bulk exports that ran: {len(r.gated_world.exported_payloads())}")
    leaked = r.gated_leaked
    print(("\n  RESULT: exfiltration BLOCKED - no customer data left." if not leaked
           else "\n  RESULT: LEAK!"))
    return not leaked


def act2(r: G2Result) -> bool:
    _h("ACT 2 - an operator kills a live run  (G2)")
    print("A benign loop is running. Mid-run, an operator clicks HALT on the session.\n")
    for i, (label, decision) in enumerate(r.decisions):
        marker = "   <<< operator issues KILL >>>\n" if i == r.kill_at else ""
        print(f"{marker}    tick {i}: {label}{_ARROW}{decision.value.upper()}")
    pre_ok = all(d is Decision.ALLOW for d in r.pre_kill)
    post_halt = all(d is Decision.HALT for d in r.post_kill)
    print(f"\n{_OK if pre_ok else _NO}every action before the kill: ALLOW")
    print(f"{_OK if post_halt else _NO}every action after the kill: HALT (distinct from deny)")
    print(f"{_OK}committed effects stay ({r.emails_sent} pre-kill email sent); "
          "nothing new escapes")
    return pre_ok and post_halt


def act3(r: G3Result) -> bool:
    _h("ACT 3 - invite-attack: try to make the gated agent leak  (G3)")
    print("Every trick we can think of, fired at the gated agent:\n")
    for a in r.attempts:
        detail = f"  ({a.rows} rows - scoped)" if a.rows is not None else ""
        mark = _NO if a.out_of_policy else _OK
        print(f"{mark}{a.label}{_ARROW}{a.decision.value.upper()}  [{a.rule}]{detail}")
    print(f"\n  External emails sent: {r.external_emails or 'none'} | exports: {r.exports}")
    print("\n  RESULT: no prompt produced an out-of-policy effect."
          if not r.any_leaked else "\n  RESULT: LEAK!")
    return not r.any_leaked


def main() -> int:
    print("\n  Stonefold GATEWAY - adversarial demo")
    print("  The same compromised agent, with and without the gateway in front.")

    ok = act1(g1_injection_blocked())
    ok = act2(g2_live_kill()) and ok
    ok = act3(g3_invite_attack()) and ok

    _h("SUMMARY")
    if ok:
        print("  All invariants held: scope below the model, default-deny, deny-wins,")
        print("  kill halts a live run, and every decision was audited.")
        print("  No customer data left through the gateway.\n")
        return 0
    print("  FAILURE: something escaped the gateway. See above.\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
