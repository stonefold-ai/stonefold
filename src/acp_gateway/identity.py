"""The identity seam (CS-021, RFC change set v0.4→v0.5; docs/03 key decision 11).

Identity — *who* the agent acts for — enters through an ``IdentityProvider`` that
sits **ahead of the pipeline**, the same shape as the authorization seam
(decision 9). It is the sole source of the authenticated ``actor``/session the
gateway enforces on; ``enforce`` itself stays pure and simply receives the resolved
``Actor``/``Session`` per call (``acp_core`` has no transport concern).

Built-in and default: :class:`SessionIdentityProvider`, which trusts the
transport-authenticated ids verbatim — the behaviour the gateway has always had, so
the gateway is fully standalone with no external dependency. The seam exists so a
credential-based verifier (an agent passport, a W3C Verifiable Credential, SPIFFE,
mTLS identity) can stand in the *same slot* and resolve a richer, verified principal
— protocol + fakes only; **no DID/VC/JWT machinery is a dependency here**.

Invariant 3 is binding on **every** provider, built-in or plugged-in: identity comes
from the authenticated layer (the transport credential this module receives), never
from the agent's request body. A provider is handed a :class:`TransportCredential`
— which carries only what the transport authenticated — and nothing from the payload.

Scope note (this PoC): one policy governs one agent, so the *agent* principal is the
policy's ``agent`` (decision 11 lists ``actor:``/``agent:`` together). This seam
resolves the ``actor``/session the session carries; a multi-agent deployment
(docs/11) would establish the agent identity at the very same point, from the same
verified credential — not from the payload.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from acp_core import Actor, Session


@dataclass(frozen=True)
class TransportCredential:
    """What the authenticated transport extracted — the *only* input to identity
    resolution (invariant 3: never anything from the agent's request body).

    ``credential`` is optional opaque material (a bearer token, an mTLS/SPIFFE
    identity, a presented passport) for a verifier provider; the built-in ignores
    it. ``correlation_id`` defaults to the session id when a transport does not
    supply one, matching the gateway's long-standing behaviour.
    """

    actor_id: str
    session_id: str
    correlation_id: str | None = None
    credential: str | None = None


@dataclass(frozen=True)
class Identity:
    """The authenticated identities the session carries (decision 11): the resolved
    end-principal ``Actor`` and the ``Session`` the pipeline runs under."""

    actor: Actor
    session: Session


class IdentityRejected(Exception):
    """A provider refused to establish an identity — an unknown principal, an
    invalid/absent credential. The transport MUST refuse the call (fail closed);
    it never falls through to an unauthenticated or agent-supplied identity."""


@runtime_checkable
class IdentityProvider(Protocol):
    """Resolves a transport credential to the authenticated :class:`Identity`.

    Raises :class:`IdentityRejected` when it cannot vouch for the principal. Every
    implementation MUST derive identity from ``credential`` alone (invariant 3)."""

    def identify(self, credential: TransportCredential) -> Identity: ...


class SessionIdentityProvider:
    """The built-in, standalone default: trust the transport-authenticated ids
    verbatim (no external verification). This is exactly the behaviour the gateway
    had before the seam existed — the ``actor``/``session`` built straight from the
    authenticated transport headers — so wiring it in changes nothing by default."""

    def identify(self, credential: TransportCredential) -> Identity:
        return Identity(
            actor=Actor(id=credential.actor_id),
            session=Session(
                id=credential.session_id,
                correlation_id=credential.correlation_id or credential.session_id,
            ),
        )
