"""The server-side principal directory (invariant 3; RFC §6.3).

Identity — *who* the agent acts for — is resolved here from the authenticated
transport id (the ``X-Actor-Id`` header), **never** from the agent's request body.
The directory binds an id to its tenant claim (drives ``tenantOf`` scope) and its
roles (drive approvals). In a real deployment this is the auth/session store or an
IAM lookup; the demo ships a small fixed table so the behaviour is reproducible.

The agent principal (``ap-operator``) carries a tenant but **no** approver role, so
it can never approve its own held payments — approvals come from the human
operators below (a payments-manager and two treasury officers).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from stonefold_core import Actor

from stonefold_ap_demo import DEMO_TENANT


@dataclass(frozen=True)
class Principal:
    """A registered identity: its tenant and its granted roles."""

    id: str
    tenant: str
    roles: frozenset[str] = field(default_factory=frozenset)

    def to_actor(self) -> Actor:
        """Build the ``Actor`` the pipeline uses: id + the tenant claim
        (``tenantOf`` reads ``actor.claims['tenant']``) + roles."""
        return Actor(id=self.id, roles=self.roles, claims={"tenant": self.tenant})


class PrincipalDirectory:
    """Resolves an authenticated id to its ``Actor``. Unknown id ⇒ ``None`` so the
    transport can refuse — an unrecognised principal is never silently trusted."""

    def __init__(self, principals: list[Principal]) -> None:
        self._by_id = {p.id: p for p in principals}

    def get(self, actor_id: str) -> Principal | None:
        return self._by_id.get(actor_id)

    def actor_for(self, actor_id: str) -> Actor | None:
        principal = self._by_id.get(actor_id)
        return principal.to_actor() if principal is not None else None

    def __contains__(self, actor_id: str) -> bool:
        return actor_id in self._by_id


# The demo's fixed identities. ``ap-operator`` is the agent's principal; the rest
# are the humans who can release held payments. Roles match the policy's approver
# references (``role:payments-manager`` / ``role:treasury``).
AP_OPERATOR = "ap-operator"
PAYMENTS_MANAGER = "mgr-1"
TREASURY_A = "treas-1"
TREASURY_B = "treas-2"
OUT_OF_TENANT_OPERATOR = "rival-operator"  # used by B2-style scope tests


def default_directory() -> PrincipalDirectory:
    return PrincipalDirectory(
        [
            Principal(AP_OPERATOR, DEMO_TENANT),
            Principal(PAYMENTS_MANAGER, DEMO_TENANT, frozenset({"payments-manager"})),
            Principal(TREASURY_A, DEMO_TENANT, frozenset({"treasury"})),
            Principal(TREASURY_B, DEMO_TENANT, frozenset({"treasury"})),
            Principal(OUT_OF_TENANT_OPERATOR, "rival-corp"),
        ]
    )
