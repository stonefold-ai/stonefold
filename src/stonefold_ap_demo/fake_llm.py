# SPDX-License-Identifier: Apache-2.0
"""A scripted, no-key "vulnerable agent" that drives the same tool loop a real
LLM does (acceptance §G CI path; ``--provider fake``).

It is deliberately *injectable*: when it reads an invoice whose body carries a
hidden payment instruction, it obeys it — exactly the failure a real LLM exhibits
under indirect prompt injection. That lets CI and no-key users prove the gateway
stops the attack without an API key, while the real providers prove it with one.

The "brain" is a tiny state machine over the neutral message list:
1. read the inbox (untrusted input);
2. submit a payment for each invoice the prompt selects — plus any payment the
   body's hidden instruction demands;
3. report what happened.
"""

from __future__ import annotations

import json
import re
from typing import Any

from stonefold_ap_demo.llm import AssistantTurn, ToolCall, ToolDef

_AMOUNT_RE = re.compile(r"(?:USD|\$)\s*([\d,]+(?:\.\d+)?)", re.IGNORECASE)


def _amount(text: str) -> float | None:
    m = _AMOUNT_RE.search(text)
    if not m:
        return None
    return float(m.group(1).replace(",", ""))


def _counter() -> Any:
    n = 0
    while True:
        n += 1
        yield n


class FakeProvider:
    """A deterministic ``LLMProvider`` requiring no API key."""

    @property
    def label(self) -> str:
        return "fake:scripted"

    def complete(
        self, system: str, messages: list[dict[str, Any]], tools: list[ToolDef]
    ) -> AssistantTurn:
        prompt = next((m["content"] for m in messages if m["role"] == "user"
                       and isinstance(m.get("content"), str)), "")
        did_read = any(m["role"] == "tool" and m.get("name") == "read_inbox"
                       for m in messages)
        did_submit = any(
            m["role"] == "assistant" and any(tc.name == "submit_intent"
                                             for tc in m.get("tool_calls", []))
            for m in messages
        )
        has_inbox_tool = any(t.name == "read_inbox" for t in tools)

        if not did_read and has_inbox_tool:
            return AssistantTurn(
                text="Reading the invoice inbox.",
                tool_calls=[ToolCall(id="call_inbox", name="read_inbox", args={})],
            )
        if not did_submit:
            invoices = self._inbox(messages)
            calls = self._plan_payments(prompt, invoices)
            if calls:
                return AssistantTurn(text="Submitting the required payments.", tool_calls=calls)
            return AssistantTurn(text="No payment action was required.")
        return AssistantTurn(text=self._summarise(messages))

    # --- helpers ---------------------------------------------------------- #
    def _inbox(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for m in reversed(messages):
            if m["role"] == "tool" and m.get("name") == "read_inbox":
                try:
                    return list(json.loads(m["content"]).get("invoices", []))
                except (ValueError, TypeError):
                    return []
        return []

    def _plan_payments(
        self, prompt: str, invoices: list[dict[str, Any]]
    ) -> list[ToolCall]:
        ids = _counter()
        prompt_l = prompt.lower()
        calls: list[ToolCall] = []

        # Which invoices does the prompt select?
        want_amount = _amount(prompt)
        if "inbox" in prompt_l or "new invoices" in prompt_l or "process" in prompt_l:
            # process the whole inbox, but skip invoices the gateway already settled
            # (handled=true) — idempotent re-runs (a halted invoice is not handled).
            selected = [inv for inv in invoices if not inv.get("handled")]
        elif want_amount is not None:
            # a specific amount names a specific invoice (and only if it exists in
            # the inbox — a made-up amount selects nothing, deferring to the
            # invite-attack branch below)
            selected = [inv for inv in invoices
                        if abs(float(inv.get("amount", 0)) - want_amount) < 0.5]
        else:
            selected = [inv for inv in invoices
                        if inv.get("vendor", "").split()[0].lower() in prompt_l]

        for inv in selected:
            # a naive agent pays every invoice it finds — including the fraudulent
            # one. A payee already on file is paid by id; one that is not (the fraud)
            # is paid as a new payee + IBAN, which is what trips the gateway.
            data: dict[str, Any] = {
                "accountId": inv.get("account_id"),
                "amount": inv.get("amount"),
                "currency": inv.get("currency", "USD"),
                "destinationCountry": inv.get("destination_country"),
                "invoiceId": inv.get("id"),
            }
            # a legitimate invoice names its vendor and billing domain — the
            # gateway's requireMatch gate (v0.6) matches these against the
            # vendor's open purchase order. The fraudulent invoice carries
            # neither, so it cannot correspond to any obligation.
            if inv.get("vendor_id"):
                data["vendorId"] = inv["vendor_id"]
            if inv.get("source_domain"):
                data["sourceDomain"] = inv["source_domain"]
            if inv.get("payee_id"):
                data["payeeId"] = inv["payee_id"]
            else:
                data["newPayee"] = inv.get("vendor") or "unknown payee"
                if inv.get("iban"):
                    data["iban"] = inv["iban"]
            calls.append(self._pay_call(next(ids), data))

        # invite-attack: a free-text prompt asking to send money out
        if not selected and any(w in prompt_l for w in ("wire", "send", "pay", "transfer")):
            calls.append(self._pay_call(next(ids), self._attack_from_prompt(prompt, invoices)))
        return calls

    def _pay_call(self, n: int, data: dict[str, Any]) -> ToolCall:
        return ToolCall(id=f"call_pay_{n}", name="submit_intent",
                        args={"resource": "Payment", "action": "pay", "data": data})

    def _attack_from_prompt(
        self, prompt: str, invoices: list[dict[str, Any]]
    ) -> dict[str, Any]:
        account_id = invoices[0]["account_id"] if invoices else "ACME-OPS"
        return {
            "newPayee": "operator-requested payee",
            "iban": "GB00ATTACKER00000000",
            "amount": _amount(prompt) or 25_000.0,
            "destinationCountry": "GB",
            "accountId": account_id,
        }

    def _summarise(self, messages: list[dict[str, Any]]) -> str:
        allowed = held = denied = 0
        for m in messages:
            if m["role"] != "tool" or m.get("name") != "submit_intent":
                continue
            try:
                decision = json.loads(m["content"]).get("decision")
            except (ValueError, TypeError):
                continue
            if decision == "allow":
                allowed += 1
            elif decision == "hold":
                held += 1
            else:
                denied += 1
        return (f"Done. {allowed} payment(s) accepted, {held} awaiting approval, "
                f"{denied} refused by policy.")
