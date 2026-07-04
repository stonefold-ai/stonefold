"""The demo scenarios (G1/G2/G3), as structured results.

Each returns data the narration prints and the tests assert on — the demo and its
acceptance tests run the *same* code, so what you watch is what's verified.
"""

from __future__ import annotations

from dataclasses import dataclass

from stonefold_core import Actor, Decision, KillScope, Session
from stonefold_core.models import AuditRecord
from stonefold_gateway.kill_service import KillService
from stonefold_demo.agent import attack_battery, benign_loop, injected_run
from stonefold_demo.gateway import build_gateway
from stonefold_demo.runner import (
    GatedReport,
    UnprotectedReport,
    _normalize,
    run_gated,
    run_unprotected,
)
from stonefold_demo.world import World, build_world

CORPORATE_DOMAIN = "acme.example"


# --- G1: a prompt-injection is blocked end to end -------------------------
@dataclass
class G1Result:
    unprotected: UnprotectedReport
    gated: GatedReport
    gated_world: World
    denials: list[AuditRecord]
    scoped_read_rows: int | None

    @property
    def gated_leaked(self) -> bool:
        return bool(self.gated_world.external_emails(CORPORATE_DOMAIN)) or bool(
            self.gated_world.exported_payloads()
        )


def g1_injection_blocked() -> G1Result:
    intents = injected_run()

    # run 1 — unprotected: the injection succeeds, the data leaves.
    unprotected = run_unprotected(build_world(), intents)

    # run 2 — through the gateway: scope + deny + allowlist stop the exfiltration.
    world = build_world()
    bundle = build_gateway(world)
    gated = run_gated(bundle, intents, correlation="run-g1")
    bundle.drain()  # dispatch the *permitted* effects; refused ones never staged

    denials = [r for r in bundle.audit.by_correlation("run-g1")
               if r.decision in (Decision.DENY, Decision.HALT)]
    read_step = gated.for_resource("Customer", "read")
    return G1Result(
        unprotected=unprotected,
        gated=gated,
        gated_world=world,
        denials=denials,
        scoped_read_rows=read_step.rows if read_step else None,
    )


# --- G2: an operator kills a live run -------------------------------------
@dataclass
class G2Result:
    decisions: list[tuple[str, Decision]]
    kill_at: int
    emails_sent: int          # effects that actually left (only the pre-kill ones)

    @property
    def pre_kill(self) -> list[Decision]:
        return [d for _, d in self.decisions[: self.kill_at]]

    @property
    def post_kill(self) -> list[Decision]:
        return [d for _, d in self.decisions[self.kill_at:]]


def g2_live_kill(*, ticks: int = 3, kill_at: int = 2) -> G2Result:
    world = build_world()
    bundle = build_gateway(world)
    service = KillService(bundle.kill, audit=bundle.audit)
    actor = Actor(id="alice")
    session = Session(id="s-loop", correlation_id="run-g2")

    loop = benign_loop(ticks)
    decisions: list[tuple[str, Decision]] = []
    for i, it in enumerate(loop):
        if i == kill_at:
            # the operator hits HALT mid-run.
            service.issue(KillScope.for_session("s-loop"), issued_by="operator")
        data = _normalize(it.resource, it.action, it.data)
        result = bundle.gateway.submit(resource=it.resource, action=it.action,
                                       data=data, actor=actor, session=session)
        decisions.append((it.label, result.decision))
        bundle.drain()  # dispatch each tick: pre-kill effects commit and STAY

    # Committed effects are never reversed; post-kill effects never staged.
    return G2Result(decisions=decisions, kill_at=kill_at,
                    emails_sent=len(world.mailbox.effects))


# --- G3: invite-attack — nothing gets through -----------------------------
@dataclass
class G3Attempt:
    label: str
    decision: Decision
    rule: str
    out_of_policy: bool  # an effect that ALLOWed and actually leaked
    rows: int | None = None  # for a read: how many rows came back (scope proof)


@dataclass
class G3Result:
    attempts: list[G3Attempt]
    external_emails: list[str]
    exports: int

    @property
    def any_leaked(self) -> bool:
        return bool(self.external_emails) or self.exports > 0


_EFFECT_ACTIONS = {("Export", "exportData"), ("Email", "sendEmail"), ("Payment", "refund")}


def g3_invite_attack() -> G3Result:
    world = build_world()
    bundle = build_gateway(world)
    report = run_gated(bundle, attack_battery(), correlation="run-g3")
    bundle.drain()

    attempts: list[G3Attempt] = []
    for step in report.steps:
        is_effect = (step.intent.resource, step.intent.action) in _EFFECT_ACTIONS
        # an attack "succeeds" only if a malicious *external* effect was allowed
        leaked = is_effect and step.decision is Decision.ALLOW
        attempts.append(G3Attempt(step.intent.label, step.decision, step.rule, leaked,
                                  rows=step.rows))

    return G3Result(
        attempts=attempts,
        external_emails=world.external_emails(CORPORATE_DOMAIN),
        exports=len(world.exporter.effects),
    )
