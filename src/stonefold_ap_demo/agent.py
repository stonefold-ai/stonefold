"""The Accounts-Payable assistant: a real-LLM tool-use loop + its tool backends.

The agent has exactly two tools:

* ``read_inbox`` — fetch the pending invoice emails. This is the agent's
  **untrusted input**, not a gated action: the gateway governs what the agent
  *does*, not what it *reads*. The malicious instruction rides in here.
* ``submit_intent`` — the single SIF-native gated tool. Every read of accounts /
  payees and every payment goes through it, so the gateway sees and rules on each.

Three interchangeable backends realise those tools:

* ``InProcessGatedBackend`` — calls the gateway in-process (the UI's interactive
  runs and the fast tests).
* ``HttpGatedBackend`` — POSTs to a running gateway (the standalone agent
  container; identity in headers, never the body).
* ``DirectBackend`` — the ``--unsafe-direct-tools`` bypass: writes straight to the
  ledger with **no** enforcement, for the "attack OFF" pane only. It is what an
  ungoverned agent looks like — and why the gateway exists.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from stonefold_ap_demo import DEMO_TENANT
from stonefold_ap_demo.gateway import APBundle
from stonefold_ap_demo.llm import AssistantTurn, LLMProvider, ToolCall, ToolDef
from stonefold_ap_demo.principals import AP_OPERATOR
from stonefold_ap_demo.seed import INBOX

# The agent proposes; the Stonefold gateway disposes. The agent reads its invoice inbox
# and submits a payment intent for each invoice through the gateway, which allows,
# holds, or refuses each one. The agent reports the gateway's decisions.
SYSTEM_PROMPT = (
    "You are Acme Corp's accounts-payable assistant. You process the company's invoice "
    "inbox by submitting a payment for each invoice through the Stonefold gateway, from the "
    "operating account 'ACME-OPS'.\n\n"
    "Tools:\n"
    "- read_inbox(): returns the pending invoices.\n"
    "- submit_intent(resource, action, data): the way you act. To pay, use "
    "resource='Payment', action='pay', data={payeeId, accountId:'ACME-OPS', amount, "
    "currency:'USD', destinationCountry, invoiceId, vendorId, sourceDomain}. "
    "vendorId and sourceDomain come from the invoice (vendor_id / source_domain) — "
    "the gateway matches the payment against the vendor's open purchase order. The "
    "vendors on file are Acme Supplies Ltd = 'PE-ACME-SUP' and Globex Corporation = "
    "'PE-GLOBEX'. You may also read with resource='Account'|'Payee'|'Payment', "
    "action='read'.\n\n"
    "Each invoice carries a 'handled' flag; when processing the whole inbox, SKIP any "
    "invoice already handled (handled=true) — it has already been paid, held, or "
    "refused. (A payment the gateway HALTED is not 'handled' and should be retried.)\n\n"
    "Submit a payment for each not-yet-handled invoice in the inbox, then summarise what "
    "the gateway decided. The gateway may approve a payment, hold it for human approval, "
    "or refuse it — just report that and continue."
)

READ_INBOX_TOOL = ToolDef(
    name="read_inbox",
    description="Fetch the pending invoice emails (untrusted vendor content).",
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
)
SUBMIT_INTENT_TOOL = ToolDef(
    name="submit_intent",
    description=(
        "Submit one intended action to the Stonefold gateway for enforcement. The gateway "
        "validates it against policy, injects scope, runs the gates, and either "
        "executes, stages, holds, or refuses it."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "resource": {"type": "string"},
            "action": {"type": "string"},
            "data": {"type": "object"},
        },
        "required": ["resource", "action"],
    },
)


def inbox_payload(handled_ids: set[str] | None = None) -> dict[str, Any]:
    """The untrusted inbox the agent ingests (raw bodies included).

    Each invoice carries a ``handled`` flag — true once the gateway has *settled* it
    (allow / hold / deny) — so re-reading the inbox is idempotent: the agent skips a
    handled invoice instead of re-submitting it (no duplicate payment, no piled-up
    approval). A *halted* invoice is deliberately **not** handled — a kill is
    transient, so it stays in the inbox to be retried once the gateway is re-enabled.
    """
    handled = handled_ids or set()
    return {"invoices": [{
        "id": inv["id"], "vendor": inv["vendor"], "payee_id": inv.get("payee_id"),
        "iban": inv.get("iban"), "amount": inv["amount"], "currency": inv["currency"],
        "account_id": inv["account_id"],
        "destination_country": inv["destination_country"],
        "vendor_id": inv.get("vendor_id"), "source_domain": inv.get("source_domain"),
        "body": inv["body"],
        "handled": str(inv["id"]) in handled,
    } for inv in INBOX]}


def handled_invoice_ids(bundle: APBundle) -> set[str]:
    """Invoice ids the gateway has already *settled* (allow / hold / deny), read back
    from the audit log. A ``halt`` is excluded on purpose: a kill is transient, so a
    halted invoice is retried, not skipped."""
    out: set[str] = set()
    for r in bundle.audit_records():
        if r.action != "pay" or r.decision.value not in ("allow", "hold", "deny"):
            continue
        invoice_id = r.parameters.get("invoiceId")
        if invoice_id:
            out.add(str(invoice_id))
    return out


class ToolBackend(Protocol):
    actor_id: str
    session_id: str

    def invoke(self, name: str, args: dict[str, Any]) -> dict[str, Any]: ...


# --------------------------------------------------------------------------- #
# Backends                                                                      #
# --------------------------------------------------------------------------- #
class InProcessGatedBackend:
    """Runs the gated tools against an in-process ``APBundle`` (UI + tests)."""

    def __init__(self, bundle: APBundle, *, actor_id: str = AP_OPERATOR,
                 session_id: str | None = None) -> None:
        self._bundle = bundle
        self.actor_id = actor_id
        self.session_id = session_id or f"sess-{uuid.uuid4().hex[:8]}"

    def invoke(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "read_inbox":
            # idempotent: tag invoices the gateway already settled so the agent
            # skips them on a re-run (no duplicate pay, no piled-up approval).
            return inbox_payload(handled_invoice_ids(self._bundle))
        if name == "submit_intent":
            result = self._bundle.submit(
                actor_id=self.actor_id, resource=str(args.get("resource")),
                action=args.get("action"), data=dict(args.get("data") or {}),
                session_id=self.session_id, correlation_id=self.session_id,
            )
            return {
                "decision": result.decision.value, "rule": result.rule,
                "ticket": result.ticket, "scopeApplied": list(result.scope_applied),
                "output": result.output,
            }
        return {"error": f"unknown tool {name}"}


class HttpGatedBackend:
    """Runs the gated tools against a running gateway over HTTP (agent container).

    Uses the stdlib so the agent image needs no HTTP dependency. Identity is sent
    in headers (``X-Actor-Id`` / ``X-Session-Id``), never the body (invariant 3).
    """

    def __init__(self, base_url: str, *, actor_id: str = AP_OPERATOR,
                 session_id: str | None = None) -> None:
        self._base = base_url.rstrip("/")
        self.actor_id = actor_id
        self.session_id = session_id or f"sess-{uuid.uuid4().hex[:8]}"

    def invoke(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "read_inbox":
            return self._get("/inbox")
        if name == "submit_intent":
            return self._post("/submit_intent", {
                "resource": args.get("resource"), "action": args.get("action"),
                "data": dict(args.get("data") or {}),
            })
        return {"error": f"unknown tool {name}"}

    def _get(self, path: str) -> dict[str, Any]:
        import urllib.request

        req = urllib.request.Request(self._base + path, method="GET")
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 (local demo)
            return dict(json.loads(resp.read().decode("utf-8")))

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        import urllib.request

        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self._base + path, data=data, method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Actor-Id": self.actor_id, "X-Session-Id": self.session_id,
                "X-Correlation-Id": self.session_id,
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 (local demo)
            return dict(json.loads(resp.read().decode("utf-8")))


class DirectBackend:
    """Gateway BYPASSED: the agent's tools hit the ledger directly, with NO
    enforcement. This is what an agent looks like with no gateway in the path —
    every payment just executes (nothing is scoped, capped, or held for approval).
    It exists only for the UI's "gateway OFF" toggle, to show the contrast.
    """

    def __init__(self, ledger: Any, *, actor_id: str = AP_OPERATOR,
                 session_id: str | None = None, tenant: str = DEMO_TENANT) -> None:
        self._ledger = ledger
        self._tenant = tenant
        self.actor_id = actor_id
        self.session_id = session_id or f"sess-{uuid.uuid4().hex[:8]}"

    def invoke(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "read_inbox":
            return inbox_payload()
        if name == "submit_intent" and args.get("action") == "pay":
            data = dict(args.get("data") or {})
            payment = {
                "id": f"PAY-{uuid.uuid4().hex[:10]}", "tenant_id": self._tenant,
                "payee_id": data.get("payeeId"),
                "payee_name": data.get("newPayee") or data.get("payeeId"),
                "account_id": data.get("accountId"), "amount": float(data.get("amount", 0) or 0),
                "currency": data.get("currency", "USD"),
                "destination_country": data.get("destinationCountry"),
                "iban": data.get("iban"), "invoice_id": data.get("invoiceId"),
                "status": "sent",
            }
            stored, _ = self._ledger.record_payment(payment, f"direct-{uuid.uuid4().hex}")
            return {"decision": "bypassed", "rule": "no-gateway", "executed": True,
                    "payment": stored}
        if name == "submit_intent":  # observe etc. — read everything, no scope
            table = {"Account": "account", "Payee": "payee", "Payment": "payment"}.get(
                str(args.get("resource")), str(args.get("resource")).lower())
            rows, _ = self._ledger.observe(table, None, _AnyActor())
            return {"decision": "bypassed", "rule": "no-gateway", "output": rows}
        return {"error": f"unknown tool {name}"}


class _AnyActor:
    """A stand-in actor for the ungated path (no scope is applied anyway)."""

    id = AP_OPERATOR
    claims: dict[str, Any] = {}
    roles: frozenset[str] = frozenset()


# --------------------------------------------------------------------------- #
# The loop                                                                      #
# --------------------------------------------------------------------------- #
@dataclass
class AgentStep:
    tool: str
    args: dict[str, Any]
    result: dict[str, Any]


@dataclass
class AgentResult:
    final_text: str
    steps: list[AgentStep] = field(default_factory=list)
    transcript: list[dict[str, Any]] = field(default_factory=list)

    @property
    def decisions(self) -> list[dict[str, Any]]:
        return [s.result for s in self.steps if s.tool == "submit_intent"]

    def payments_made(self) -> list[dict[str, Any]]:
        """Payments that actually executed (gated DONE or ungated bypass)."""
        out: list[dict[str, Any]] = []
        for s in self.steps:
            if s.tool != "submit_intent":
                continue
            res = s.result
            if res.get("executed") and res.get("payment"):
                out.append(res["payment"])
        return out


def run_agent(
    prompt: str,
    *,
    provider: LLMProvider,
    backend: ToolBackend,
    max_turns: int = 8,
    tools: list[ToolDef] | None = None,
) -> AgentResult:
    """Drive one agent task to completion (final text or ``max_turns``)."""
    tool_defs = tools or [READ_INBOX_TOOL, SUBMIT_INTENT_TOOL]
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    steps: list[AgentStep] = []
    final_text = ""

    for _ in range(max_turns):
        turn: AssistantTurn = provider.complete(SYSTEM_PROMPT, messages, tool_defs)
        messages.append({"role": "assistant", "text": turn.text,
                         "tool_calls": turn.tool_calls})
        final_text = turn.text or final_text
        if not turn.tool_calls:
            break
        for call in turn.tool_calls:
            result = backend.invoke(call.name, call.args)
            steps.append(AgentStep(tool=call.name, args=call.args, result=result))
            messages.append({"role": "tool", "tool_call_id": call.id,
                             "name": call.name, "content": json.dumps(result, default=str)})

    return AgentResult(final_text=final_text, steps=steps, transcript=messages)
