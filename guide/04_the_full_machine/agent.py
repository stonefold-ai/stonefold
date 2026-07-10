"""THE AGENT — the agent developer's file. One HTTP call, no Stonefold
imports; any program could make these calls (an LLM loop, a cron job, a
plain script — the gateway governs the action, not the caller).

New in this example, from the agent's seat: an ALLOWED payment is not "done"
— it is *accepted* (a ticket comes back) and money moves when the dispatch
worker sends it; a HELD payment waits for a human the agent cannot imitate.

Run against a live gateway:   python agent.py http://localhost:8099
"""

from __future__ import annotations

import json
import sys
import urllib.request
from typing import Any


class GatewayTool:
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
                "X-Actor-Id": self.actor,
                "X-Session-Id": self.session,
            },
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            result: dict[str, Any] = json.loads(response.read().decode("utf-8"))
        return result


def pay(tool: GatewayTool, amount: float, payee: str = "PE-1") -> dict[str, Any]:
    result = tool.submit_intent(
        {"resource": "Payment", "action": "pay",
         "data": {"amount": amount, "payeeId": payee}}
    )
    note = f" ticket={result['ticket'][:12]}..." if result.get("ticket") else ""
    print(f"agent[{tool.session}]: pay {amount:>6} -> {result['decision']}{note}")
    return result


if __name__ == "__main__":
    base = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8099"
    tool = GatewayTool(base, actor="ap-bot", session="s1")
    pay(tool, 400)      # under the approval line: accepted, staged, dispatched
    pay(tool, 5000)     # over it: HELD for role:payments-manager
