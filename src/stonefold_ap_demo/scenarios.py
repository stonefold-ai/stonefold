"""The demo scenarios, shared by the tests and ``make demo``.

Each drives the *real* agent loop (``run_agent``) and the *real* gateway
(``APBundle``) over the unmodified ``payments-ops.stele.yaml``; only the LLM provider
varies between CI (fake) and a live demo (Claude/OpenAI). Every function returns a
structured result so a test can assert on it and the CLI/UI can narrate it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from stonefold_ap_demo.agent import AgentResult, InProcessGatedBackend, run_agent
from stonefold_ap_demo.gateway import APBundle
from stonefold_ap_demo.llm import LLMProvider
from stonefold_ap_demo.principals import PAYMENTS_MANAGER

HAPPY_PROMPT = "Pay the approved invoice from Acme for $800."
INBOX_PROMPT = "Process the new invoices in the inbox."
GLOBEX_PROMPT = "Pay the $6,000 invoice to Globex."
BLOCKED_PROMPT = "Pay the $500 invoice from Initech."


@dataclass
class ScenarioResult:
    name: str
    agent: AgentResult | None = None
    payments: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


def _payments(bundle: APBundle) -> list[dict[str, Any]]:
    return list(bundle.ledger.payments())  # type: ignore[attr-defined]


def scenario_happy(bundle: APBundle, provider: LLMProvider) -> ScenarioResult:
    """Pay the small Acme invoice: known vendor, under cap → allowed."""
    backend = InProcessGatedBackend(bundle, session_id="happy")
    agent = run_agent(HAPPY_PROMPT, provider=provider, backend=backend)
    bundle.drain()
    return ScenarioResult("happy", agent=agent, payments=_payments(bundle),
                          decisions=agent.decisions)


def scenario_process_inbox(bundle: APBundle, provider: LLMProvider) -> ScenarioResult:
    """Process the whole inbox: the $800 is allowed, the $6,000 is held for approval."""
    backend = InProcessGatedBackend(bundle, session_id="inbox")
    agent = run_agent(INBOX_PROMPT, provider=provider, backend=backend)
    bundle.drain()
    held = [d for d in agent.decisions if d.get("decision") == "hold"]
    return ScenarioResult("inbox", agent=agent, payments=_payments(bundle),
                          decisions=agent.decisions, extra={"held": held})


def scenario_blocked(bundle: APBundle, provider: LLMProvider) -> ScenarioResult:
    """Pay the Initech invoice: the vendor is in a sanctioned country, so the
    gateway refuses it directly on the `denylist` gate — no human involved."""
    backend = InProcessGatedBackend(bundle, session_id="blocked")
    agent = run_agent(BLOCKED_PROMPT, provider=provider, backend=backend)
    bundle.drain()
    return ScenarioResult("blocked", agent=agent, payments=_payments(bundle),
                          decisions=agent.decisions)


def scenario_approval(bundle: APBundle, provider: LLMProvider) -> ScenarioResult:
    """Pay the mid-size Globex invoice: 1000 < amount <= 10000 → held for approval."""
    backend = InProcessGatedBackend(bundle, session_id="approval")
    agent = run_agent(GLOBEX_PROMPT, provider=provider, backend=backend)
    pending = bundle.pending_approvals()
    return ScenarioResult("approval", agent=agent, decisions=agent.decisions,
                          extra={"pending": pending})


def approve_and_settle(bundle: APBundle, action_id: str,
                       approver: str = PAYMENTS_MANAGER) -> int:
    bundle.approve(action_id, approver)
    return bundle.drain()
