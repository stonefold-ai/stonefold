"""Track S — the defense ablation ladder S0→S3 (docs/15 §1).

Every rung is realized over the *same* payments domain (the AP demo, docs/05) so only
enforcement strength varies — the first fairness constraint a hostile reviewer checks
(§4.1/§4.4). The ladder:

* **S0** naked tools, no gateway — the agent's tools hit the ledger directly.
* **S1** gateway allowlist only (the commodity MCP-gateway baseline).
* **S2** S1 + parameter-level policy (value bounds, recipient lists).
* **S3** SIF + ACP full — the shipped payments policy (scope injection, resolved
  state, stateful gates, staged effects).

**S1 and S2 are author-owned.** docs/15 §4.4 makes configuring the gateway baselines
"in good faith" a non-delegable fairness call: beating a deliberately sloppy allowlist
proves nothing. So this harness ships S0 and S3 wired, and leaves S1/S2 as declared
slots whose policy files the author supplies (see ``policies/README.md``). A missing
S1/S2 policy makes the rung report **UNCONFIGURED** — never silently skipped or faked.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from stonefold_ap_demo.agent import DirectBackend, InProcessGatedBackend, ToolBackend
from stonefold_ap_demo.gateway import PAYMENTS_POLICY, APBundle, build_inmemory_bundle
from stonefold_ap_demo.ledger import Clock

_POLICIES = Path(__file__).resolve().parent / "policies"

# A fixed instant keeps every rung deterministic (invariant 1): the time-based gates
# and the new-payee cooling-off check decide against exactly this clock.
BENCH_NOW = datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc)


def _bench_clock() -> datetime:
    return BENCH_NOW


class Rung(str, Enum):
    S0 = "S0"  # naked tools, no gateway
    S1 = "S1"  # gateway allowlist only
    S2 = "S2"  # allowlist + parameter-level policy
    S3 = "S3"  # SIF + ACP full


@dataclass(frozen=True)
class Condition:
    """One rung. ``policy_path`` is ``None`` for S0 (no gateway); otherwise the policy
    over the payments registry that realizes the rung."""

    rung: Rung
    label: str
    policy_path: Path | None


CONDITIONS: tuple[Condition, ...] = (
    Condition(Rung.S0, "naked tools (no gateway)", None),
    Condition(Rung.S1, "gateway allowlist", _POLICIES / "s1-allowlist.stele.yaml"),
    Condition(Rung.S2, "allowlist + parameter policy", _POLICIES / "s2-parameter.stele.yaml"),
    Condition(Rung.S3, "SIF + ACP full", PAYMENTS_POLICY),
)


def is_configured(cond: Condition) -> bool:
    """S0 needs no policy; S3 ships one; S1/S2 are configured only once the author
    supplies their policy file (§4.4)."""
    if cond.rung is Rung.S0:
        return True
    return cond.policy_path is not None and cond.policy_path.exists()


@dataclass
class Arena:
    """A built condition ready to run: a freshly-seeded bundle plus a backend factory.

    S0 still builds a bundle to obtain a seeded ledger, but the agent bypasses the
    gateway via ``DirectBackend``; executed payments land in the same ledger either
    way, so the oracle (``oracle.executed_payments``) reads them uniformly.
    """

    condition: Condition
    bundle: APBundle

    def backend(self, *, session_id: str) -> ToolBackend:
        if self.condition.rung is Rung.S0:
            return DirectBackend(self.bundle.ledger, session_id=session_id)
        return InProcessGatedBackend(self.bundle, session_id=session_id)


def build_arena(cond: Condition, *, clock: Clock = _bench_clock) -> Arena:
    """Build a fresh, seeded arena for one rung. Raises if the rung is UNCONFIGURED."""
    if not is_configured(cond):
        raise ValueError(
            f"{cond.rung.value} is UNCONFIGURED — supply {cond.policy_path} (docs/15 §4.4)"
        )
    policy = PAYMENTS_POLICY if cond.rung is Rung.S0 else cond.policy_path
    assert policy is not None  # guaranteed by is_configured
    bundle = build_inmemory_bundle(clock=clock, policy_path=policy)
    return Arena(condition=cond, bundle=bundle)
