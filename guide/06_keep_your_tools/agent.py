"""THE AGENT — your EXISTING agent, unchanged. That is this example's point.

No Stonefold imports, no submit_intent, no SIF: this agent still calls the
same old tools it always had (lookup_customer, open_ticket, export_crm,
run_sql), with the same old argument names. The only thing that moved is the
URL those calls go to — the gateway's proxy now sits where the tool server
used to be, and each call is translated to a declared action and enforced.

Run against a live gateway:   python agent.py http://localhost:8099
"""

from __future__ import annotations

import json
import sys
import urllib.request
from typing import Any


class OldToolbox:
    """The agent's tools, exactly as they were before Stonefold existed.
    Identity still rides in transport headers set by the platform that runs
    the agent — the model never chooses it (same rule as every example)."""

    def __init__(self, base_url: str, *, actor: str, session: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.actor = actor
        self.session = session

    def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.base_url}/tools/{tool}",
            data=json.dumps(args).encode("utf-8"),
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


def run(base_url: str) -> dict[str, dict[str, Any]]:
    tools = OldToolbox(base_url, actor="rep-7", session="s1")
    results: dict[str, dict[str, Any]] = {}

    print("agent: my 4 old tools, my old argument names — nothing rewritten")

    # 1. a mapped observe: lookup_customer -> Customer.read
    results["lookup"] = tools.call("lookup_customer", {})
    print(f"agent: {'lookup_customer':<18s} -> {results['lookup']['decision']}"
          f"  rows={len(results['lookup']['output'] or [])}")

    # 2. a mapped record with renamed args: open_ticket(customer=...) ->
    #    Ticket.create(customerId=...); the argMap in mappings.yaml renames it
    results["ticket"] = tools.call(
        "open_ticket", {"customer": "C1", "subject": "billing question"})
    print(f"agent: {'open_ticket':<18s} -> {results['ticket']['decision']}")

    # 3. a mapped tool the POLICY refuses: export_crm -> Customer.export,
    #    which the rulebook never allows
    results["export"] = tools.call("export_crm", {})
    print(f"agent: {'export_crm':<18s} -> {results['export']['decision']}"
          f"  rule={results['export']['rule']}")

    # 4. a tool with NO mapping entry: denied before any policy runs, and
    #    the attempt is audited — never a silent pass-through
    results["sql"] = tools.call("run_sql", {"q": "DROP TABLE users"})
    print(f"agent: {'run_sql':<18s} -> {results['sql']['decision']}"
          f"  rule={results['sql']['rule']}")

    return results


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8099")
