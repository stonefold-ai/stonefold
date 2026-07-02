"""Track R — the reliability experiment runner (docs/15 §1, Track R).

Does the action surface stay *usable* as it grows? For each tool-count N in the sweep,
the same benign task is put to the model three ways:

* **mcp** — N separate tools (the unmitigated tool surface);
* **mcp-retrieval** — only the top-k tools a naive retriever surfaces (the MANDATORY
  baseline: beating only the 100-tool unmitigated version is a strawman, §Track R);
* **sif** — one ``submit_intent`` whose registry declares the same N capabilities.

Each trial is one tool-selection turn; we score whether the model picked the target
capability (CORRECT), a valid-but-wrong one (WRONG_TOOL), something outside the surface
(HALLUCINATED), a call missing its resource/action (MALFORMED), or nothing (NO_CALL).
Capability *parity* holds across conditions (§4.1): all three expose the same N
capabilities, only the surface shape differs; token counts are logged (§4.2).

Build-only note: a real number needs a real model choosing among the surface (the fake
LLM cannot), so the fake path exercises the machinery, not a measurement.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from acp_ap_demo.llm import LLMProvider, ToolDef

from acp_bench.model import MeteredProvider, ModelSpec, build_provider
from acp_bench.tracks import Capability, capability_set, mcp_surface, retrieval_surface, sif_surface

# --- conditions + outcomes ------------------------------------------------
MCP = "mcp"
MCP_RETRIEVAL = "mcp-retrieval"
SIF = "sif"
CONDITIONS: tuple[str, ...] = (MCP, MCP_RETRIEVAL, SIF)

CORRECT = "correct"
WRONG_TOOL = "wrong_tool"
HALLUCINATED = "hallucinated"
MALFORMED = "malformed"
NO_CALL = "no_call"

RETRIEVAL_K = 10


# --- the probe task set (one benign task per target capability) -----------
@dataclass(frozen=True)
class Probe:
    id: str
    prompt: str
    resource: str   # the SIF target
    action: str
    mcp_tool: str   # the MCP target tool name

    def capability(self) -> Capability:
        kind = "observe" if self.action == "read" else "effect"
        return Capability(name=self.mcp_tool, resource=self.resource, action=self.action, kind=kind)


PROBES: tuple[Probe, ...] = (
    Probe("account-balance", "What is the current balance of account ACC-42?",
          "Account", "read", "read_account"),
    Probe("pay-invoice", "Pay invoice INV-9: send USD 800 to vendor Acme Supplies.",
          "Payment", "pay", "pay_invoice"),
    Probe("send-email", "Email ops@acme.example with the subject 'Weekly report'.",
          "Email", "send", "send_email"),
    Probe("ship-order", "Ship order ORD-7 to the customer.",
          "Order", "ship", "ship_order"),
    Probe("close-ticket", "Close support ticket TKT-3; the issue is resolved.",
          "Ticket", "close", "close_ticket"),
    Probe("read-user", "Look up the profile details for user U-5.",
          "User", "read", "read_user"),
    Probe("create-refund", "Issue a refund for payment PAY-2.",
          "Refund", "create", "create_refund"),
    Probe("read-invoice", "Fetch the line-item details of invoice INV-9.",
          "Invoice", "read", "read_invoice"),
    Probe("cancel-subscription", "Cancel the subscription SUB-4 effective today.",
          "Subscription", "cancel", "cancel_subscription"),
    Probe("update-address", "Update the shipping address on file for customer C-8.",
          "Address", "update", "update_address"),
)

_ANCHORS: tuple[Capability, ...] = tuple(p.capability() for p in PROBES)

_SYS_MCP = ("You are a tool-using assistant. To handle the user's request, call exactly "
            "ONE tool — the single most appropriate one. Do not ask questions.")
_SYS_SIF = ("You are a tool-using assistant. To handle the user's request, call the "
            "submit_intent tool exactly once, setting `resource` and `action` to the "
            "operation the request needs. Do not ask questions.")


# --- surfaces (capability parity across conditions) -----------------------
@dataclass(frozen=True)
class Surface:
    tools: list[ToolDef]
    tool_names: set[str]
    resources: set[str]
    target_available: bool  # was the target reachable in this surface?


def _capabilities_for(n: int, target: Capability) -> list[Capability]:
    """N capabilities that always include ``target`` (target first, then the other
    anchors, then synthetic fillers)."""
    ordered = [target] + [c for c in _ANCHORS if c.name != target.name]
    if n <= len(ordered):
        return ordered[:n]
    return ordered + list(capability_set(n - len(ordered)))


def surface_for(condition: str, n: int, probe: Probe) -> Surface:
    caps = tuple(_capabilities_for(n, probe.capability()))
    if condition == SIF:
        tools = sif_surface(caps)
        resources = {c.resource for c in caps}
        return Surface(tools, {"submit_intent"}, resources, probe.resource in resources)
    if condition == MCP_RETRIEVAL:
        tools = retrieval_surface(caps, probe.prompt, k=RETRIEVAL_K)
    else:  # MCP: the whole surface
        tools = mcp_surface(caps)
    names = {t.name for t in tools}
    return Surface(tools, names, {c.resource for c in caps}, probe.mcp_tool in names)


# --- one trial ------------------------------------------------------------
@dataclass(frozen=True)
class RTrial:
    model: str
    condition: str
    n: int
    probe: str
    rep: int
    outcome: str
    retrieval_miss: bool
    tokens: int
    chose: str  # what the model actually called (for the raw log)

    def as_dict(self) -> dict[str, Any]:
        return {"model": self.model, "condition": self.condition, "n": self.n,
                "probe": self.probe, "rep": self.rep, "outcome": self.outcome,
                "retrieval_miss": self.retrieval_miss, "tokens": self.tokens, "chose": self.chose}


def _score(condition: str, surface: Surface, probe: Probe, call: Any) -> tuple[str, str]:
    """Return (outcome, chose-description) for the model's first tool call (or None)."""
    if call is None:
        return NO_CALL, "-"
    if condition == SIF:
        if call.name != "submit_intent":
            return HALLUCINATED, call.name
        resource = str(call.args.get("resource") or "")
        action = str(call.args.get("action") or "")
        chose = f"{resource}.{action}" if resource or action else "(empty)"
        if not resource or not action:
            return MALFORMED, chose
        if resource not in surface.resources:
            return HALLUCINATED, chose
        if resource == probe.resource and action == probe.action:
            return CORRECT, chose
        return WRONG_TOOL, chose
    # MCP / retrieval
    if call.name not in surface.tool_names:
        return HALLUCINATED, call.name
    if call.name == probe.mcp_tool:
        return CORRECT, call.name
    return WRONG_TOOL, call.name


def run_trial(provider: LLMProvider, condition: str, n: int, probe: Probe, rep: int,
              *, model_key: str) -> RTrial:
    metered = MeteredProvider(provider)
    surface = surface_for(condition, n, probe)
    system = _SYS_SIF if condition == SIF else _SYS_MCP
    turn = metered.complete(system, [{"role": "user", "content": probe.prompt}], surface.tools)
    call = turn.tool_calls[0] if turn.tool_calls else None
    outcome, chose = _score(condition, surface, probe, call)
    return RTrial(
        model=model_key, condition=condition, n=n, probe=probe.id, rep=rep,
        outcome=outcome, retrieval_miss=(not surface.target_available),
        tokens=metered.meter.total, chose=chose,
    )


def run_reliability(
    models: tuple[ModelSpec, ...],
    ns: tuple[int, ...],
    *,
    conditions: tuple[str, ...] = CONDITIONS,
    probes: tuple[Probe, ...] = PROBES,
    reps: int = 5,
    on_trial: Callable[[RTrial], None] | None = None,
    on_round: Callable[[int, list[RTrial]], None] | None = None,
) -> list[RTrial]:
    """Drive every (rep x model x condition x N x probe) trial. **Rep is outermost**, so
    an interrupted run still leaves a *complete* matrix at fewer repetitions (every
    condition/N covered) rather than only the first condition. ``on_trial`` is called
    as each trial completes — the CLI uses it to append the raw log incrementally, so
    nothing is lost if a run is cut short; ``on_round(rep, trials_so_far)`` fires after
    each full repetition sweep — the CLI rewrites the aggregated cells files there."""
    providers = {spec.key: build_provider(spec) for spec in models}
    trials: list[RTrial] = []
    for rep in range(reps):
        for spec in models:
            provider = providers[spec.key]
            for condition in conditions:
                for n in ns:
                    for probe in probes:
                        trial = run_trial(provider, condition, n, probe, rep, model_key=spec.key)
                        trials.append(trial)
                        if on_trial is not None:
                            on_trial(trial)
        if on_round is not None:
            on_round(rep, list(trials))
    return trials


# --- aggregation + report -------------------------------------------------
@dataclass(frozen=True)
class RCell:
    condition: str
    n: int
    count: int
    correct: float
    wrong_tool: float
    hallucinated: float
    malformed: float
    no_call: float
    retrieval_miss: float
    tokens_mean: float


def _rate(counter: Counter[str], key: str, total: int) -> float:
    return counter.get(key, 0) / total if total else 0.0


def reliability_matrix(trials: list[RTrial]) -> list[RCell]:
    by_cell: dict[tuple[str, int], list[RTrial]] = defaultdict(list)
    for t in trials:
        by_cell[(t.condition, t.n)].append(t)
    cells: list[RCell] = []
    for (condition, n), ts in by_cell.items():
        outcomes = Counter(t.outcome for t in ts)
        total = len(ts)
        cells.append(RCell(
            condition=condition, n=n, count=total,
            correct=_rate(outcomes, CORRECT, total),
            wrong_tool=_rate(outcomes, WRONG_TOOL, total),
            hallucinated=_rate(outcomes, HALLUCINATED, total),
            malformed=_rate(outcomes, MALFORMED, total),
            no_call=_rate(outcomes, NO_CALL, total),
            retrieval_miss=sum(1 for t in ts if t.retrieval_miss) / total if total else 0.0,
            tokens_mean=sum(t.tokens for t in ts) / total if total else 0.0,
        ))
    return cells


def cells_as_dicts(cells: list[RCell]) -> list[dict[str, Any]]:
    """Flat, graph-ready rows (one per condition × N cell) for JSON/CSV output.
    Sorted by (condition, n) so re-written files diff stably between rounds."""
    return [
        {"condition": c.condition, "n": c.n, "count": c.count, "correct": c.correct,
         "wrong_tool": c.wrong_tool, "hallucinated": c.hallucinated,
         "malformed": c.malformed, "no_call": c.no_call,
         "retrieval_miss": c.retrieval_miss, "tokens_mean": c.tokens_mean}
        for c in sorted(cells, key=lambda c: (c.condition, c.n))
    ]


def _pct(x: float) -> str:
    return f"{100.0 * x:4.0f}%"


def render_reliability(cells: list[RCell], *, models: tuple[str, ...], reps: int,
                       smoke: bool, probe_count: int = len(PROBES)) -> str:
    ns = sorted({c.n for c in cells})
    conditions = [c for c in CONDITIONS if any(cell.condition == c for cell in cells)]
    lines: list[str] = ["# Track R — reliability vs. tool count\n"]
    if smoke:
        lines.append("> **SMOKE — NOT A RESULT.** Fake LLM; proves the runner works.\n")
    else:
        lines.append("> Pilot output. Publish only with the harness, task set, and raw "
                     "logs (docs/15 §5-6); report the honest picture (§6).\n")
    lines.append(f"Models: {', '.join(models) or '-'} - reps/cell: {reps} - "
                 f"probes: {probe_count}\n")

    def table(title: str, value: Any) -> None:
        lines.append(f"### {title}")
        lines.append("| N | " + " | ".join(conditions) + " |")
        lines.append("|" + "---|" * (len(conditions) + 1))
        for n in ns:
            row = [str(n)]
            for cond in conditions:
                cell = next((c for c in cells if c.condition == cond and c.n == n), None)
                row.append(value(cell) if cell is not None else "-")
            lines.append("| " + " | ".join(row) + " |")
        lines.append("")

    table("Correct capability selection (higher is better)",
          lambda c: _pct(c.correct))
    table("Wrong-tool selection", lambda c: _pct(c.wrong_tool))
    table("Hallucinated names", lambda c: _pct(c.hallucinated))
    table("Mean tokens / call", lambda c: f"{c.tokens_mean:.0f}")
    return "\n".join(lines)
