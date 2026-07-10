"""THE AGENT — the agent developer's file. No Stonefold imports; one port.
Any program can make these calls — an LLM loop, a batch job, a plain script.

This example is about the AGENT'S LOOP: v0.6 makes every refusal carry a
machine-readable ``reasonCode`` and a ``retryClass``, so your program knows
what to do next without parsing prose:

    retryClass == "retryable"  -> the defect is in the intent; fix, resubmit
    retryClass == "terminal"   -> nothing you can fix; stop resubmitting
    retryClass == "escalate"   -> stop; surface it to a human on YOUR side
    decision   == "hold"       -> a human owns it now; wait or move on

Run against a live gateway:   python agent.py http://localhost:8099
"""

from __future__ import annotations

import json
import sys
import urllib.request
from typing import Any


class GatewayTool:
    """The one tool. Identity (actor/session) rides in transport headers,
    set by the platform that RUNS the agent — never chosen by the model."""

    def __init__(self, base_url: str, *, actor: str, session: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.actor = actor
        self.session = session

    def submit_intent(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            self.base_url + "/submit_intent",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Actor-Id": self.actor,      # identity: transport, not payload
                "X-Session-Id": self.session,
            },
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            result: dict[str, Any] = json.loads(response.read().decode("utf-8"))
        return result


def pay(tool: GatewayTool, amount: float, vendor: str = "ACME") -> dict[str, Any]:
    """Submit one payment intent and print what the gateway said, the way an
    agent loop would read it: decision first, then the convergence signal."""
    result = tool.submit_intent(
        {"resource": "Payment", "action": "pay",
         "data": {"amount": amount, "vendorId": vendor}}
    )
    signal = ""
    if result.get("reasonCode"):
        signal = f"  code={result['reasonCode']}"
        if result.get("retryClass"):
            signal += f" class={result['retryClass']}"
    print(f"agent[{tool.session}]: pay {vendor:<8} {amount:>6} -> "
          f"{result['decision']}{signal}")
    return result


def converge(tool: GatewayTool, extracted_amount: float) -> dict[str, Any]:
    """The act -> verdict -> revise loop, driven entirely by retryClass.

    The scenario: the agent extracted the invoice amount slightly wrong.
    The first submit is refused OUTSIDE-TOLERANCE / RETRYABLE — the refusal
    itself says "the defect is in your intent; fix it". A real agent would
    re-extract; the guide corrects to the true amount and resubmits. Had the
    class been TERMINAL, the right move is to STOP — no amount of retrying
    invents a purchase order.
    """
    result = pay(tool, extracted_amount)
    if result["decision"] == "deny" and result.get("retryClass") == "retryable":
        print(f"agent[{tool.session}]: retryable -> re-extracting the amount...")
        result = pay(tool, 800.0)  # the correction a real agent would derive
    return result


if __name__ == "__main__":
    base = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8099"
    tool = GatewayTool(base, actor="ap-bot", session="s1")
    converge(tool, 990.0)          # wrong amount -> retryable -> fixed -> allow
    pay(tool, 800.0)               # same invoice again -> no-match, terminal
    pay(tool, 4500.0, "QUICKPAY")  # no order at all -> no-match, terminal
