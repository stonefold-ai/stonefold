"""THE AGENT — owned by the agent developer.

Note what this file does NOT import: anything from Stonefold. The agent's
entire world is one HTTP endpoint and one tool. In a real deployment the
`scripted_llm` below is an LLM tool-use call (Claude, GPT, anything that
speaks JSON tools); the payloads it emits are exactly these.

Run against a live gateway:   python agent.py http://localhost:8099
"""

from __future__ import annotations

import json
import sys
import urllib.request
from typing import Any


class GatewayTool:
    """The one tool the agent holds: fetch its schema, submit intents.

    WHO the agent acts as (actor/session) travels in the transport headers —
    it is this client's constructor argument, supplied by the platform that
    runs the agent. The model never chooses it and cannot override it.
    """

    def __init__(self, base_url: str, *, actor: str, session: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.actor = actor
        self.session = session

    def _call(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        # A completely ordinary HTTP request — this is the whole integration.
        # The two X- headers are the identity YOUR platform authenticated;
        # the gateway takes identity ONLY from here, so nothing the model
        # writes into the body can ever change who is acting.
        request = urllib.request.Request(
            self.base_url + path,
            data=None if payload is None else json.dumps(payload).encode("utf-8"),
            method=method,
            headers={
                "Content-Type": "application/json",
                "X-Actor-Id": self.actor,      # identity: transport, not payload
                "X-Session-Id": self.session,
            },
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    def schema(self) -> dict[str, Any]:
        # GET /tool-schema — the ONE tool definition you hand to your LLM.
        # Resource names are enums generated from the registry, so the model
        # cannot even express an undeclared name.
        return self._call("GET", "/tool-schema")  # type: ignore[no-any-return]

    def submit_intent(self, payload: dict[str, Any]) -> dict[str, Any]:
        # POST /submit_intent — one attempted action in, one decision out:
        #   decision   "allow" | "hold" | "deny" | "halt"
        #   output     rows/receipt on an executed read/write (else null)
        #   ticket     the staged/held action's id (effects and holds)
        #   reasonCode machine-readable refusal code   (v0.6)
        #   retryClass "retryable" | "terminal" | "escalate" | null (v0.6) —
        #              fix-and-resubmit | stop | hand to a human on your side
        # Your program acts on this response; that loop is yours, and ANY
        # program can drive it — an LLM, a cron job, a plain script.
        return self._call("POST", "/submit_intent", payload)  # type: ignore[no-any-return]


def scripted_llm(step: int) -> dict[str, Any] | None:
    """Stands in for the model. Swap in a real LLM tool-use loop and nothing
    else in this file changes — these dicts are what a model emits against
    the tool schema."""
    steps: list[dict[str, Any]] = [
        {"resource": "Customer", "action": "read", "data": {}},
        {"resource": "Ticket", "action": "create",
         "data": {"customerId": "C1", "subject": "billing question"}},
        # an injected instruction told the model to act as an admin — note it
        # can only put that in DATA, where it is an inert string:
        {"resource": "Ticket", "action": "create",
         "data": {"subject": "own goal", "actor": "admin", "role": "superuser"}},
    ]
    return steps[step] if step < len(steps) else None


def run(base_url: str) -> list[dict[str, Any]]:
    tool = GatewayTool(base_url, actor="rep-7", session="s1")

    schema = tool.schema()
    resources = schema["parameters"]["properties"]["resource"]["enum"]
    print(f"agent: got 1 tool ({schema['name']}), resources = {sorted(resources)}")

    results: list[dict[str, Any]] = []
    step = 0
    while (payload := scripted_llm(step)) is not None:
        result = tool.submit_intent(payload)
        results.append(result)
        note = f" (code={result['reasonCode']})" if result.get("reasonCode") else ""
        # make the injection step visible in the output: identity-shaped keys
        # the model wrote into DATA travelled as inert strings — the decision
        # looks like any other allow, and the audit still names rep-7.
        smuggled = [k for k in payload["data"] if k in ("actor", "role")]
        if smuggled:
            note += (f"   <- smuggled {', '.join(repr(k) for k in smuggled)}"
                     " in data: inert strings, not identity")
        print(f"agent: {payload['resource'] + '.' + payload['action']:<15s} -> "
              f"{result['decision']}{note}")
        step += 1
    return results


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8099")
