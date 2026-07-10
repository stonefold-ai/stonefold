"""THE AGENT — the agent developer's file. No Stonefold imports; one port.

What the agent experiences when the policy uses registered functions: rows
it shouldn't see simply aren't in the response, blocked content comes back
as a deny with a code, and a judgment-shaped case comes back as a HOLD —
"a human owns this now" — never a guess.

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


def run(base_url: str) -> dict[str, dict[str, Any]]:
    tool = GatewayTool(base_url, actor="alice", session="s1")
    results: dict[str, dict[str, Any]] = {}

    # scope: alice asks for ALL notes; the injected filter answers.
    results["read"] = tool.submit_intent(
        {"resource": "Note", "action": "read", "data": {}}
    )
    print(f"agent: Note.read          -> {results['read']['decision']}"
          f"  rows={len(results['read']['output'] or [])} (of 2 in the table)")

    # content hook: a payload with a secret in it.
    results["blocked"] = tool.submit_intent(
        {"resource": "Note", "action": "create", "data": {"text": "the SECRET plan"}}
    )
    print(f"agent: Note.create        -> {results['blocked']['decision']}"
          f"  rule={results['blocked']['rule']}")

    # precondition check, three verdicts against three world states:
    for order_id in ("O1", "O2", "O3"):
        result = tool.submit_intent(
            {"resource": "Order", "action": "ship", "data": {"id": order_id}}
        )
        results[order_id] = result
        extra = f"  code={result['reasonCode']}" if result.get("reasonCode") else ""
        extra += f" class={result['retryClass']}" if result.get("retryClass") else ""
        print(f"agent: Order.ship {order_id}     -> {result['decision']}{extra}")

    return results


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8099")
