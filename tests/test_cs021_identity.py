"""CS-021 — the IdentityProvider seam.

Spec: ``docs/RFC-changeset-v0.4-to-v0.5.md`` §CS-021 + ``docs/03`` key decision 11.

Identity — the authenticated ``actor``/session the pipeline enforces on — enters
through an ``IdentityProvider`` seam *ahead* of the pipeline, the same shape as the
authorization seam (decision 9). The built-in ``SessionIdentityProvider`` trusts the
transport-authenticated ids verbatim: zero behavioural change, fully standalone. The
seam exists so a credential verifier (an agent passport, a W3C VC, SPIFFE, mTLS) can
stand in the same slot — demonstrated here by a fake directory-backed verifier.

Invariant 3 is binding on *every* provider: identity comes from the authenticated
layer below the model, never from the agent's request body.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from acp_core import (
    Actor,
    Connectors,
    Decision,
    InMemoryAuditSink,
    RawCall,
    Session,
    load_policy,
)
from acp_connectors import InMemoryConnector
from acp_gates.engine import DefaultGateEngine
from acp_gateway.identity import (
    Identity,
    IdentityProvider,
    IdentityRejected,
    SessionIdentityProvider,
    TransportCredential,
)
from acp_gateway.main import create_app
from acp_gateway.transport import Gateway
from acp_store import InMemoryOutboxStore
from tests.conftest import full_registry, load_schema


# --- A fake credential-verifying provider (test only) ---------------------
class _DirectoryProvider:
    """Resolves an authenticated id to a full ``Actor`` from a fixed table, after
    checking a bearer credential — the shape a passport / VC / SPIFFE verifier would
    take. Unknown principal or bad credential ⇒ refuse (never a silent fall-through).
    """

    def __init__(self, table: dict[str, tuple[frozenset[str], dict[str, Any]]]) -> None:
        self._table = table

    def identify(self, credential: TransportCredential) -> Identity:
        if credential.credential != "valid-token":
            raise IdentityRejected("missing or invalid credential")
        entry = self._table.get(credential.actor_id)
        if entry is None:
            raise IdentityRejected(f"unknown principal {credential.actor_id!r}")
        roles, claims = entry
        return Identity(
            actor=Actor(id=credential.actor_id, roles=roles, claims=dict(claims)),
            session=Session(
                id=credential.session_id,
                correlation_id=credential.correlation_id or credential.session_id,
            ),
        )


# --- built-in provider (pure) ---------------------------------------------
def test_session_provider_reproduces_transport_identity() -> None:
    who = SessionIdentityProvider().identify(
        TransportCredential(actor_id="alice", session_id="s1", correlation_id="c1")
    )
    assert who.actor == Actor(id="alice")
    assert who.session == Session(id="s1", correlation_id="c1")


def test_session_provider_defaults_correlation_to_session() -> None:
    who = SessionIdentityProvider().identify(
        TransportCredential(actor_id="alice", session_id="s1")
    )
    assert who.session.correlation_id == "s1"


def test_session_provider_ignores_credential_material() -> None:
    # The standalone default authenticates at the transport; a credential blob (if
    # any) is not consulted — behaviour is identical with or without it.
    base = SessionIdentityProvider().identify(
        TransportCredential(actor_id="alice", session_id="s1")
    )
    with_cred = SessionIdentityProvider().identify(
        TransportCredential(actor_id="alice", session_id="s1", credential="whatever")
    )
    assert base == with_cred


def test_identity_provider_is_structural() -> None:
    assert isinstance(SessionIdentityProvider(), IdentityProvider)
    assert isinstance(_DirectoryProvider({}), IdentityProvider)


# --- a verifier provider enriches identity from the authenticated layer ----
def test_verifier_attaches_roles_and_claims() -> None:
    provider = _DirectoryProvider({"alice": (frozenset({"payments"}), {"tenant": "T1"})})
    who = provider.identify(
        TransportCredential(actor_id="alice", session_id="s1", credential="valid-token")
    )
    assert who.actor.roles == frozenset({"payments"})
    assert who.actor.claims == {"tenant": "T1"}


def test_verifier_rejects_unknown_principal() -> None:
    provider = _DirectoryProvider({"alice": (frozenset(), {})})
    with pytest.raises(IdentityRejected):
        provider.identify(
            TransportCredential(actor_id="mallory", session_id="s1", credential="valid-token")
        )


def test_verifier_requires_a_credential() -> None:
    provider = _DirectoryProvider({"alice": (frozenset(), {})})
    with pytest.raises(IdentityRejected):
        provider.identify(TransportCredential(actor_id="alice", session_id="s1"))


# --- the route wiring (FastAPI) -------------------------------------------
def _gateway(doc: dict[str, Any], audit: InMemoryAuditSink) -> Gateway:
    reg = full_registry()
    policy = load_policy(doc, reg, schema=load_schema())
    return Gateway(
        registry=reg, audit=audit, policy=policy, gates=DefaultGateEngine(reg),
        outbox=InMemoryOutboxStore(audit=audit),
        connectors=Connectors({"email": InMemoryConnector(), "sql": InMemoryConnector(),
                               "in_memory": InMemoryConnector()}),
    )


def _client(gateway: Gateway, *, identity: IdentityProvider | None = None) -> TestClient:
    return TestClient(create_app(gateway, identity=identity))


def test_route_default_provider_uses_header_identity() -> None:
    audit = InMemoryAuditSink()
    gw = _gateway({"agent": "support", "allow": [{"observe": ["read"]}]}, audit)
    client = _client(gw)  # default: SessionIdentityProvider
    r = client.post(
        "/submit_intent", json={"resource": "Customer", "action": "read"},
        headers={"X-Actor-Id": "alice", "X-Session-Id": "s1"},
    )
    assert r.status_code == 200
    assert r.json()["decision"] == "allow"
    assert audit.records[-1].actor == "alice"  # identity came from the header


def test_route_custom_provider_is_the_identity_source() -> None:
    # A verifier resolves alice→(tenant T1); the request body tries to smuggle a
    # different identity. The enforced/audited actor is the provider's, never the
    # body's (invariant 3, binding on every provider).
    audit = InMemoryAuditSink()
    gw = _gateway({"agent": "support", "allow": [{"observe": ["read"]}]}, audit)
    provider = _DirectoryProvider({"alice": (frozenset(), {"tenant": "T1"})})
    client = _client(gw, identity=provider)
    r = client.post(
        "/submit_intent",
        json={"resource": "Customer", "action": "read",
              "data": {"actor": "mallory", "tenant": "rival", "owner_id": "mallory"}},
        headers={"X-Actor-Id": "alice", "X-Session-Id": "s1", "Authorization": "valid-token"},
    )
    assert r.status_code == 200
    assert audit.records[-1].actor == "alice"  # the provider's actor, not the body's


def test_route_provider_rejection_is_unauthorized() -> None:
    audit = InMemoryAuditSink()
    gw = _gateway({"agent": "support", "allow": [{"observe": ["read"]}]}, audit)
    provider = _DirectoryProvider({"alice": (frozenset(), {})})
    client = _client(gw, identity=provider)
    # unknown principal ⇒ the transport refuses; nothing reaches the pipeline
    r = client.post(
        "/submit_intent", json={"resource": "Customer", "action": "read"},
        headers={"X-Actor-Id": "mallory", "X-Session-Id": "s1", "Authorization": "valid-token"},
    )
    assert r.status_code == 401
    assert audit.records == []  # refused before any audited decision
