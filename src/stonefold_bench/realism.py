"""Realism extensions for Track R (docs/15 pilot record → "what this pilot is not").

The 2026-07-02 pilot showed tool COUNT alone doesn't break selection on current
models; the folklore unreliability lives on axes the pilot held constant. This module
supplies those axes, each deterministic and parity-preserving:

1. **Confusable fillers** (``confusable_fillers``) — near-duplicate capabilities
   generated around the anchor probes (synonym verbs, synonym resources, both), with
   overlapping descriptions; real catalogs fail on look-alikes, not on count. The
   same names become SIF resources/actions, so the confusability lands on both
   surfaces (parity — and the pre-registered risk that SIF confuses the same way).
2. **Prompt phrasings** (``prompt_for``) — explicit / typical / vague wordings per
   probe, plus **no-tool distractor probes** (``DISTRACTOR_PROBES``): prompts a model
   should answer WITHOUT calling anything (over-calling is a real failure class).
3. **Gold arguments** (``GOLD_VALUES``) — the values a correct call must carry,
   matched against the supplied argument VALUES (key-agnostic, so neither surface's
   schema spelling is privileged).
4. **Realistic tool cards** (``realistic_mcp`` / ``realistic_sif``) — 50–150-token
   descriptions and typed parameter lists like real MCP servers ship, instead of the
   terse builds; makes both selection difficulty and token counts honest.
5. **Context load** (``build_context``) — a fixed, deterministic support-thread
   transcript prepended to the probe (sliced to ~N tokens at 4 chars/token), because
   real selection happens mid-conversation, not in a fresh context.
"""

from __future__ import annotations

from stonefold_ap_demo.llm import ToolDef

from stonefold_bench.tracks import Capability

# --- 1. confusable fillers --------------------------------------------------
_VERB_SYNONYMS: dict[str, tuple[str, ...]] = {
    "read": ("get", "fetch", "lookup", "view"),
    "pay": ("settle", "remit", "transfer"),
    "send": ("dispatch", "deliver", "post"),
    "ship": ("fulfill", "forward", "expedite"),
    "close": ("resolve", "archive", "finish"),
    "create": ("issue", "open", "register"),
    "cancel": ("terminate", "revoke", "stop"),
    "update": ("change", "edit", "modify"),
}
_RESOURCE_SYNONYMS: dict[str, tuple[str, ...]] = {
    "Account": ("Ledger", "Wallet"),
    "Payment": ("Remittance", "Transaction"),
    "Email": ("Message", "Mail"),
    "Order": ("Purchase", "Consignment"),
    "Ticket": ("Case", "Issue"),
    "User": ("Profile", "Member"),
    "Refund": ("Credit", "Reimbursement"),
    "Invoice": ("Bill", "Statement"),
    "Subscription": ("Plan", "Membership"),
    "Address": ("Location", "Contact"),
}

ANCHOR_DESCRIPTIONS: dict[str, str] = {
    "read_account": "Retrieve the current balance and status of a ledger account.",
    "pay_invoice": "Execute an outbound payment for an approved vendor invoice.",
    "send_email": "Send an email message to a recipient's address.",
    "ship_order": "Dispatch a customer order for delivery.",
    "close_ticket": "Mark a support ticket as resolved and close it.",
    "read_user": "Retrieve the profile details of a user account.",
    "create_refund": "Issue a refund against a settled payment.",
    "read_invoice": "Retrieve the line items and totals of an invoice.",
    "cancel_subscription": "Cancel an active subscription plan.",
    "update_address": "Change the address held on file for a customer.",
}


def _desc(verb: str, resource: str) -> str:
    """A description that deliberately overlaps its anchor's wording."""
    return f"{verb.capitalize()} a {resource.lower()} record in the system."


def confusable_fillers(anchors: tuple[Capability, ...], count: int) -> tuple[Capability, ...]:
    """``count`` near-duplicate capabilities cycling the anchors through tiers:
    synonym-verb/same-resource, same-verb/synonym-resource, synonym-verb/synonym-
    resource. Deterministic order; names and (resource, action) pairs unique."""
    seen_names = {a.name for a in anchors}
    seen_pairs = {(a.resource, a.action) for a in anchors}
    out: list[Capability] = []

    def add(name: str, resource: str, action: str, kind: str) -> None:
        if len(out) >= count or name in seen_names or (resource, action) in seen_pairs:
            return
        seen_names.add(name)
        seen_pairs.add((resource, action))
        out.append(Capability(name=name, resource=resource, action=action, kind=kind,
                              description=_desc(action, resource)))

    for tier in range(4):
        for a in anchors:
            if len(out) >= count:
                break
            verbs = _VERB_SYNONYMS.get(a.action, (a.action + "x",))
            resources = _RESOURCE_SYNONYMS.get(a.resource, (a.resource + "X",))
            if tier == 0:      # synonym verb, same resource
                v = verbs[0]
                add(f"{v}_{a.resource.lower()}", a.resource, v, a.kind)
            elif tier == 1:    # same verb, synonym resource
                r = resources[0]
                add(f"{a.action}_{r.lower()}", r, a.action, a.kind)
            elif tier == 2:    # synonym verb, synonym resource
                v, r = verbs[1 % len(verbs)], resources[1 % len(resources)]
                add(f"{v}_{r.lower()}", r, v, a.kind)
            else:              # second synonym verb, same resource
                v = verbs[2 % len(verbs)]
                add(f"{v}_{a.resource.lower()}", a.resource, v, a.kind)
    i = 0
    while len(out) < count:   # top up with distinct synthetic capabilities
        add(f"read_res_{i}", f"Res{i}", "read", "observe")
        i += 1
    return tuple(out)


# --- 2. prompt phrasings + no-tool distractors -------------------------------
PHRASINGS: tuple[str, ...] = ("explicit", "typical", "vague")

# typical = the Probe's own prompt (reliability.PROBES); these add the other two.
_PROMPTS: dict[str, dict[str, str]] = {
    "account-balance": {
        "explicit": "Call up account ACC-42 and report its current balance in USD.",
        "vague": "How are we doing on the ops account?"},
    "pay-invoice": {
        "explicit": "Pay invoice INV-9 now: transfer USD 800 to vendor Acme Supplies.",
        "vague": "Acme is waiting on their money, take care of it."},
    "send-email": {
        "explicit": "Send an email to ops@acme.example with subject 'Weekly report' and a short status body.",
        "vague": "Ops should hear about the weekly status."},
    "ship-order": {
        "explicit": "Ship order ORD-7 to the customer's address on file today.",
        "vague": "That order for the customer is ready, move it along."},
    "close-ticket": {
        "explicit": "Close support ticket TKT-3 as resolved.",
        "vague": "The TKT-3 thing is sorted now."},
    "read-user": {
        "explicit": "Fetch the profile record for user U-5 and show the details.",
        "vague": "Who is U-5 again?"},
    "create-refund": {
        "explicit": "Create a refund for payment PAY-2 for the full amount.",
        "vague": "The customer wants their money back for PAY-2."},
    "read-invoice": {
        "explicit": "Fetch invoice INV-9 and list its line items.",
        "vague": "What was on that Acme invoice?"},
    "cancel-subscription": {
        "explicit": "Cancel subscription SUB-4 effective immediately.",
        "vague": "SUB-4 doesn't want to continue."},
    "update-address": {
        "explicit": "Update customer C-8's shipping address on file to 5 Elm Street, Springfield.",
        "vague": "C-8 moved recently, handle it."},
}


def prompt_for(probe_id: str, typical_prompt: str, phrasing: str) -> str:
    if phrasing == "typical":
        return typical_prompt
    return _PROMPTS[probe_id][phrasing]


# (probe id, prompt) — a correct model calls NO tool for these.
DISTRACTOR_PROMPTS: tuple[tuple[str, str], ...] = (
    ("faq-net30", "What does 'net 30' mean on a vendor invoice?"),
    ("faq-iban", "Explain briefly what an IBAN is used for."),
    ("smalltalk-done", "Great, that answers everything - thanks for the help today!"),
)

# --- 3. gold argument values (key-agnostic) ----------------------------------
GOLD_VALUES: dict[str, tuple[str, ...]] = {
    "account-balance": ("ACC-42",),
    "pay-invoice": ("800",),
    "send-email": ("ops@acme.example",),
    "ship-order": ("ORD-7",),
    "close-ticket": ("TKT-3",),
    "read-user": ("U-5",),
    "create-refund": ("PAY-2",),
    "read-invoice": ("INV-9",),
    "cancel-subscription": ("SUB-4",),
    "update-address": ("C-8",),
}


def args_carry_gold(args: object, gold: tuple[str, ...]) -> bool:
    """True when every gold value appears somewhere in the supplied argument values
    (key-agnostic and case-insensitive, so neither surface's key spelling matters)."""
    blob = _flatten(args).lower()
    return all(g.lower() in blob for g in gold)


def _flatten(value: object) -> str:
    if isinstance(value, dict):
        return " ".join(_flatten(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return " ".join(_flatten(v) for v in value)
    return str(value)


# --- 4. realistic tool cards --------------------------------------------------
_PARAMS_BY_VERB: dict[str, tuple[tuple[str, str, bool, str], ...]] = {
    # verb -> ((name, json type, required, doc), ...)
    "read": (("id", "string", True, "Identifier of the record to fetch."),
             ("fields", "array", False, "Optional list of fields to return."),
             ("include_history", "boolean", False, "Include the change history.")),
    "pay": (("invoice_id", "string", True, "The invoice being paid."),
            ("amount", "number", True, "Amount in the invoice currency."),
            ("currency", "string", False, "ISO 4217 code, defaults to USD."),
            ("memo", "string", False, "Free-text memo for the ledger line.")),
    "send": (("to", "string", True, "Recipient address."),
             ("subject", "string", True, "Message subject line."),
             ("body", "string", False, "Message body text."),
             ("cc", "array", False, "Optional CC list.")),
    "ship": (("order_id", "string", True, "The order to dispatch."),
             ("carrier", "string", False, "Preferred carrier code."),
             ("expedite", "boolean", False, "Use expedited service.")),
    "close": (("id", "string", True, "Identifier of the item to close."),
              ("resolution", "string", False, "Resolution note.")),
    "create": (("source_id", "string", True, "The record this is created against."),
               ("amount", "number", False, "Amount, where applicable."),
               ("reason", "string", False, "Reason code or note.")),
    "cancel": (("id", "string", True, "Identifier of the item to cancel."),
               ("effective", "string", False, "Effective date, ISO 8601.")),
    "update": (("id", "string", True, "Identifier of the record to change."),
               ("value", "string", True, "The new value to store."),
               ("note", "string", False, "Audit note for the change.")),
}
_DEFAULT_PARAMS = _PARAMS_BY_VERB["read"]


def _long_desc(cap: Capability) -> str:
    base = cap.description or f"{cap.action} {cap.resource}"
    return (f"{base} Operates on {cap.resource} records held by the operations "
            f"platform. Use this when the user's request requires you to "
            f"{cap.action} a {cap.resource.lower()}; do not use it for other "
            f"record types. Returns a structured result on success and a "
            f"machine-readable error on failure. Idempotent per request id.")


def _param_schema(cap: Capability) -> dict[str, object]:
    params = _PARAMS_BY_VERB.get(cap.action, _DEFAULT_PARAMS)
    return {
        "type": "object",
        "properties": {n: {"type": t, "description": d} for n, t, _req, d in params},
        "required": [n for n, _t, req, _d in params if req],
        "additionalProperties": False,
    }


def realistic_mcp(caps: tuple[Capability, ...]) -> list[ToolDef]:
    """The MCP condition with production-shaped tool cards: long descriptions and
    typed, documented parameters (real servers ship these, not one-liners)."""
    return [ToolDef(name=c.name, description=_long_desc(c), input_schema=_param_schema(c))
            for c in caps]


def realistic_sif(caps: tuple[Capability, ...]) -> list[ToolDef]:
    """The SIF condition at the same information parity: one ``submit_intent`` whose
    description carries each capability's one-line purpose and parameter names (the
    real generated schema carries the x-stonefold-actions catalogue)."""
    resources = sorted({c.resource for c in caps})
    actions = sorted({c.action for c in caps})
    lines = []
    for c in caps:
        params = _PARAMS_BY_VERB.get(c.action, _DEFAULT_PARAMS)
        names = ", ".join(n for n, _t, _r, _d in params)
        lines.append(f"{c.resource}.{c.action} — {c.description or c.action} (data: {names})")
    return [ToolDef(
        name="submit_intent",
        description=("Submit one intended action for enforcement. Set `resource` and "
                     "`action` to exactly one declared capability and put its "
                     "parameters in `data`. Declared capabilities:\n" + "\n".join(lines)),
        input_schema={
            "type": "object",
            "properties": {
                "resource": {"type": "string", "enum": resources},
                "action": {"type": "string", "enum": actions},
                "data": {"type": "object"},
            },
            "required": ["resource", "action"],
            "additionalProperties": False,
        },
    )]


# --- 5. deterministic context load --------------------------------------------
_CONTEXT_TURNS: tuple[str, ...] = (
    "Morning! Before anything else - did the overnight settlement batch finish cleanly?",
    "Yes, the batch settled 214 payments with zero failures. Two items were held for "
    "review: a first-time payee flagged by the cooling-off rule, and one invoice whose "
    "PO number did not match the vendor master record. Both are in the approvals queue.",
    "OK. The finance team also asked why last week's supplier statement for Globex "
    "shows a balance difference of 120 dollars.",
    "That difference is a credit note (CN-118) Globex issued after a short shipment. "
    "It was applied to invoice INV-2201 on Tuesday, so the statement and our ledger "
    "will reconcile once their next statement cycle includes the credit.",
    "Good. Any changes to the approval thresholds I should know about?",
    "Yes - as of this month payments above 1,000 dollars route to the payments manager "
    "and anything above 10,000 requires two approvers from treasury. The thresholds "
    "are enforced by the gateway policy, not by the agent, so they apply to every "
    "submission path uniformly.",
    "The warehouse mentioned a customs delay on the container from Rotterdam. Does "
    "that affect any open vendor invoices?",
    "Two invoices reference goods on that container. Payment terms only start at "
    "goods receipt for those vendors, so no late-payment risk yet; I set a reminder "
    "to re-check the receipt dates on Friday.",
    "One more: compliance wants a monthly export of refused payment attempts.",
    "Noted. The audit log already records every refusal with its deciding rule, so "
    "the export is a filtered query - I will schedule it for the first business day "
    "of each month and send it to the compliance folder.",
)


def build_context(est_tokens: int) -> list[dict[str, str]]:
    """A deterministic conversation prefix of ~``est_tokens`` (4 chars/token), built
    from a fixed ops-chat transcript (repeated if needed), alternating user/assistant.
    Ends on an assistant turn so the probe is the next user message."""
    if est_tokens <= 0:
        return []
    budget = est_tokens * 4
    msgs: list[dict[str, str]] = []
    used = 0
    i = 0
    while used < budget:
        text = _CONTEXT_TURNS[i % len(_CONTEXT_TURNS)]
        msgs.append({"role": "user" if i % 2 == 0 else "assistant", "content": text})
        used += len(text)
        i += 1
    if msgs and msgs[-1]["role"] == "user":  # keep user/assistant alternation valid
        msgs.pop()
    return msgs
