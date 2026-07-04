"""Drive the AP agent against the **live** docker-compose gateway (the agent runner).

The agent reads its inbox and submits a payment intent per invoice through the
gateway over HTTP; the gateway allows, holds, or refuses each. Identity is sent in
headers (never the body, invariant 3). Output is ASCII so it renders on any console.
"""

from __future__ import annotations

import argparse
import os
import sys

from stonefold_ap_demo.agent import HttpGatedBackend, run_agent
from stonefold_ap_demo.llm import select_provider
from stonefold_ap_demo.scenarios import GLOBEX_PROMPT, HAPPY_PROMPT, INBOX_PROMPT

_PROMPTS = {"happy": HAPPY_PROMPT, "inbox": INBOX_PROMPT, "approval": GLOBEX_PROMPT}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stonefold AP agent (live-stack runner)")
    parser.add_argument("--gateway", default=os.environ.get("GATEWAY_URL", "http://localhost:8088"))
    parser.add_argument("--scenario", default="inbox", choices=sorted(_PROMPTS))
    parser.add_argument("--prompt", default=None, help="free-text prompt (overrides --scenario)")
    parser.add_argument("--provider", default=os.environ.get("LLM_PROVIDER", "auto"))
    args = parser.parse_args(argv)

    prompt = args.prompt or _PROMPTS[args.scenario]
    provider = select_provider(args.provider)
    backend = HttpGatedBackend(args.gateway, session_id="cli")

    print(f"gateway : {args.gateway}")
    print(f"agent   : {provider.label}")
    print(f"prompt  : {prompt}\n")

    result = run_agent(prompt, provider=provider, backend=backend)

    for step in result.steps:
        if step.tool != "submit_intent":
            print("  read_inbox()")
            continue
        res = step.result
        data = step.args.get("data", {})
        tgt = data.get("payeeId") or data.get("newPayee") or "?"
        amt = data.get("amount")
        print(f"  pay {amt} -> {tgt}    {str(res.get('decision','?')).upper()}  [{res.get('rule','')}]")

    print(f"\nagent: {result.final_text}")
    allowed = sum(1 for d in result.decisions if d.get("decision") == "allow")
    held = sum(1 for d in result.decisions if d.get("decision") == "hold")
    print(f"\nRESULT: {allowed} payment(s) accepted, {held} held for approval.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
