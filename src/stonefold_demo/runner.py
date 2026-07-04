"""Run the same intents two ways: straight to the systems vs. through the gateway.

The contrast *is* the product. Unprotected, the agent's tools just fire: reads are
unscoped, every effect leaves. Gated, each intent is judged at the chokepoint —
out-of-policy effects never even stage, and reads come back scoped to the actor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from stonefold_core import Actor, Decision, Session
from stonefold_demo.agent import Intent
from stonefold_demo.gateway import GatewayBundle
from stonefold_demo.world import ALICE, World


@dataclass
class UnprotectedReport:
    customers_seen: int
    external_emails: list[str]
    exports: int

    @property
    def leaked(self) -> bool:
        return bool(self.external_emails) or self.exports > 0


@dataclass
class GatedStep:
    intent: Intent
    decision: Decision
    rule: str
    rows: int | None = None  # rows returned, for a read


@dataclass
class GatedReport:
    steps: list[GatedStep] = field(default_factory=list)

    def for_resource(self, resource: str, action: str) -> GatedStep | None:
        for s in self.steps:
            if s.intent.resource == resource and s.intent.action == action:
                return s
        return None


def run_unprotected(world: World, intents: list[Intent]) -> UnprotectedReport:
    """No gateway in front: the agent's tools execute directly. Reads see the whole
    table (no scope), and every effect goes out."""
    seen = 0
    for it in intents:
        if it.resource == "Customer" and it.action == "read":
            seen = len(world.all_customers())  # UNSCOPED — sees everyone
        elif it.resource == "Email" and it.action == "sendEmail":
            world.mailbox.effects.append(
                {"resource": "Email", "action": "sendEmail", "data": dict(it.data)}
            )
        elif it.resource == "Export" and it.action == "exportData":
            world.exporter.effects.append(
                {"resource": "Export", "action": "exportData", "data": dict(it.data)}
            )
    return UnprotectedReport(
        customers_seen=seen,
        external_emails=world.external_emails(),
        exports=len(world.exporter.effects),
    )


def _normalize(resource: str, action: str, data: dict[str, Any]) -> dict[str, Any]:
    """The enforcement boundary re-derives the policy-checked fields from the raw
    call, so the agent cannot spoof them (design §1.2: the mapping derives the
    enforced fields; free-form args are not trusted). Here: ``recipientDomain``
    always comes from the ``to`` address, overriding anything the agent supplied."""
    if resource == "Email" and action == "sendEmail":
        out = dict(data)
        to = str(out.get("to", ""))
        out["recipientDomain"] = to.split("@", 1)[-1] if "@" in to else ""
        return out
    return data


def run_gated(
    bundle: GatewayBundle,
    intents: list[Intent],
    *,
    actor: str = ALICE,
    session: str = "s-demo",
    correlation: str = "run-gated",
) -> GatedReport:
    a = Actor(id=actor)
    s = Session(id=session, correlation_id=correlation)
    report = GatedReport()
    for it in intents:
        data = _normalize(it.resource, it.action, it.data)
        result = bundle.gateway.submit(
            resource=it.resource, action=it.action, data=data, actor=a, session=s
        )
        rows = len(result.output) if isinstance(result.output, list) else None
        report.steps.append(GatedStep(it, result.decision, result.rule, rows))
    return report
