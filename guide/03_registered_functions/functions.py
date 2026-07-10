"""THE DOMAIN FUNCTIONS — owned by the function developer.

This file is where YOUR domain knowledge lives: the three kinds of small
deterministic functions the gateway calls by the names the registry declares.
Nothing here wires a gateway; that's the infra engineer's file.

House rules (the gateway holds you to them):
  * deterministic — same inputs, same answer; no model calls, ever
  * read facts from YOUR systems, never from the agent's payload
  * signal a policy verdict by RETURNING; a raised exception means
    "my dependency is down" and the gateway fails CLOSED on your behalf
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# The only Stonefold imports a function developer needs: the context your
# checks receive and the three-valued result they may return.
from stonefold_gates.base import CheckResult, GateContext, check_hold


def no_secrets(content: Mapping[str, Any]) -> bool:
    """CONTENT HOOK — called for every action gated with
    ``contentCheck: no.secrets``. True = clean, False = block."""
    return "SECRET" not in str(dict(content)).upper()


def target_is_active(ctx: GateContext) -> bool:
    """PRECONDITION CHECK, two-valued — pass iff the resolved target's
    ``status`` is active.

    ``ctx.env.resource`` holds the target's fields, resolved by the GATEWAY
    from your system of record (see gateway_service.py's env_factory) — the
    agent's payload can only say WHICH order, never what state it is in."""
    return ctx.env.resource.get("status") == "active"


def inventory_available(ctx: GateContext) -> "bool | CheckResult":
    """PRECONDITION CHECK, three-valued (v0.6) — pass, fail, or HOLD.

    A hold is for judgment-shaped ambiguity: the data was read fine and the
    honest answer is "a human should look at this". A hold MUST carry a
    machine-readable reason code; the registry declares it (with its retry
    class) so the gateway, the linter, and the agent all know what
    ``stock-uncertain`` means."""
    stock = ctx.env.resource.get("stock")
    if stock == "unknown":
        return check_hold("stock-uncertain",
                          evidence={"target": ctx.env.resource.get("id")})
    return bool(stock and int(stock) > 0)


# What the infra engineer registers, keyed by the REGISTRY-DECLARED names:
HOOKS = {"no.secrets": no_secrets}
CHECKS = {"targetIsActive": target_is_active, "inventoryAvailable": inventory_available}
