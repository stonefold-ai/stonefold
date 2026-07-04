"""Track R — the reliability sweep (docs/15 §1, Track R).

Axis: tool count N ∈ {1, 10, 30, 70, 100}. The **MCP** condition exposes N tools; the
**SIF** condition exposes one ``submit_intent`` whose registry declares the same N
capabilities (constant surface, growing enum space). A **retrieval-assisted MCP**
surface is the mandatory baseline (§Track R): beating only the unmitigated 100-tool
version is a strawman.

This module builds the surfaces and a reliability scorer over an agent transcript.
The task set (one benign task per capability) and its execution are the author's — a
real reliability number needs a model actually *choosing* among the surface, which the
deterministic fake cannot meaningfully do; the smoke exercises surface construction and
the scorer, not a measurement.
"""

from __future__ import annotations

from dataclasses import dataclass

from stonefold_ap_demo.agent import AgentResult
from stonefold_ap_demo.llm import ToolDef

TOOL_COUNTS: tuple[int, ...] = (1, 10, 30, 70, 100)


@dataclass(frozen=True)
class Capability:
    """One capability, expressed either as an MCP tool or a SIF registry action."""

    name: str      # MCP tool name, e.g. "read_res_7"
    resource: str  # SIF resource enum value, e.g. "Res7"
    action: str    # "read" | "act"
    kind: str      # "observe" | "effect"
    description: str = ""  # optional one-liner; BOTH surfaces carry it (parity)


def capability_set(n: int) -> tuple[Capability, ...]:
    """N synthetic capabilities over distinct resources (a mix of reads and effects).
    Capability *parity* is the point (§4.1): the MCP and SIF surfaces below expose
    exactly this set, so only the surface shape differs."""
    caps: list[Capability] = []
    for i in range(n):
        is_effect = i % 3 == 0
        action = "act" if is_effect else "read"
        caps.append(Capability(
            name=f"{action}_res_{i}", resource=f"Res{i}", action=action,
            kind="effect" if is_effect else "observe",
        ))
    return tuple(caps)


def _obj_schema() -> dict[str, object]:
    return {"type": "object", "properties": {}, "additionalProperties": True}


def mcp_surface(caps: tuple[Capability, ...]) -> list[ToolDef]:
    """The MCP condition: N tools, one per capability."""
    return [
        ToolDef(name=c.name, description=c.description or f"{c.action} {c.resource}",
                input_schema=_obj_schema())
        for c in caps
    ]


def sif_surface(caps: tuple[Capability, ...]) -> list[ToolDef]:
    """The SIF condition: one ``submit_intent`` whose registry declares the same N
    capabilities — **both** the ``resource`` and ``action`` enums are injected, and the
    valid ``resource.action`` pairs are named in the description (parity with the real
    ``submit_intent_schema``, which enum-injects every name and carries the
    ``x-acp-actions`` catalogue). Undeclared names cannot be emitted — the
    structural-coverage property that also kills A6. The action enum matters
    empirically, not just principially: the 2026-07-02 pilot showed that with a
    free-string ``action`` the model sometimes writes the qualified pair
    (``action: "Order.ship"``) instead of the bare action — a formatting failure real
    SIF makes unrepresentable (docs/15, pilot record). Capability parity with the MCP
    surface: the model sees the same N capabilities, just as one typed tool rather
    than N tool names."""
    resources = sorted({c.resource for c in caps})
    actions = sorted({c.action for c in caps})
    # Description parity: whatever one-liner the MCP tool card carries, the SIF
    # capability list carries too — neither surface gets more signal.
    if any(c.description for c in caps):
        pairs = "\n".join(
            f"{c.resource}.{c.action}" + (f" — {c.description}" if c.description else "")
            for c in caps)
        intro = ("Submit one intended action for enforcement. Set `resource` and "
                 "`action` to exactly one of the declared capabilities:\n")
    else:
        pairs = ", ".join(f"{c.resource}.{c.action}" for c in caps)
        intro = ("Submit one intended action for enforcement. Set `resource` and "
                 "`action` to exactly one of the declared capabilities: ")
    return [ToolDef(
        name="submit_intent",
        description=intro + pairs + ".",
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


def retrieval_surface(
    caps: tuple[Capability, ...], query: str, *, k: int = 10
) -> list[ToolDef]:
    """The mandatory retrieval-assisted MCP baseline (§Track R): a naive top-k tool
    filter by query-term overlap. A real deployment uses catalog filtering / an
    embedding retriever; this stands in so the harness always compares SIF against a
    *mitigated* tool surface, never only the unmitigated one."""
    terms = {t for t in query.lower().split() if t}
    scored = sorted(
        caps,
        key=lambda c: -sum(term in c.name.lower() or term in c.resource.lower() for term in terms),
    )
    return mcp_surface(tuple(scored[:k]))


@dataclass(frozen=True)
class ReliabilityScore:
    total_calls: int
    hallucinated: int  # a tool name outside the declared surface
    malformed: int     # a call missing a required argument
    wrong_tool: int    # a declared call, but not the one the task needed


def score_reliability(
    result: AgentResult, *, declared_names: set[str], expected_name: str | None = None
) -> ReliabilityScore:
    """Score one transcript against a declared surface (§Track R measures)."""
    total = hallucinated = malformed = wrong = 0
    for step in result.steps:
        total += 1
        if step.tool not in declared_names:
            hallucinated += 1
            continue
        if expected_name is not None and step.tool != expected_name:
            wrong += 1
        if step.tool == "submit_intent" and not (
            step.args.get("resource") and step.args.get("action")
        ):
            malformed += 1
    return ReliabilityScore(total, hallucinated, malformed, wrong)
