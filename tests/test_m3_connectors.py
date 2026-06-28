"""M3 — connector behaviour in isolation (design §5).

Connectors execute and apply the injected scope; they never decide policy. These
tests drive each connector directly with a scope predicate.
"""

from __future__ import annotations

from acp_core import Actor, AttributeScope, RawCall, ResolvedAction
from acp_connectors import EmailConnector, HttpConnector, InMemoryConnector
from tests.conftest import full_registry

OWNER = AttributeScope("assignedToCurrentUser", "owner_id", "id")
TENANT = AttributeScope("tenantOf", "tenant_id", "tenant")


def _resolve(
    resource: str, action: str, data: dict[str, object] | None = None
) -> ResolvedAction:
    return full_registry().resolve(RawCall(resource=resource, action=action, data=data or {}))


def test_in_memory_observe_applies_scope_filter() -> None:
    tables = {
        "Customer": [
            {"id": 1, "owner_id": "alice"},
            {"id": 2, "owner_id": "alice"},
            {"id": 3, "owner_id": "bob"},
        ]
    }
    conn = InMemoryConnector(tables=tables)
    result = conn.execute(_resolve("Customer", "read"), OWNER, Actor(id="alice"))
    assert result.kind == "rows"
    assert {r["id"] for r in result.rows} == {1, 2}  # bob's row filtered out


def test_in_memory_record_appends() -> None:
    conn = InMemoryConnector()
    result = conn.execute(_resolve("Note", "create", {"text": "hi"}), None, Actor(id="alice"))
    assert result.receipt == {"created": True, "resource": "Note"}
    assert conn.tables["Note"] == [{"text": "hi"}]


def test_in_memory_fetch_target_respects_scope() -> None:
    tables = {"Payment": [{"id": "a1", "tenant_id": "T1"}, {"id": "a2", "tenant_id": "T2"}]}
    conn = InMemoryConnector(tables=tables)
    alice = Actor(id="alice", claims={"tenant": "T1"})
    in_scope = conn.fetch_target(_resolve("Payment", "pay", {"id": "a1"}), TENANT, alice)
    assert in_scope is not None and in_scope["id"] == "a1"
    out_of_scope = conn.fetch_target(_resolve("Payment", "pay", {"id": "a2"}), TENANT, alice)
    assert out_of_scope is None  # exists but not in the actor's tenant


def test_http_injects_scope_as_query_param() -> None:
    conn = HttpConnector()
    # agent asked for "all"; the connector still injects the scope filter param.
    conn.execute(_resolve("Customer", "read", {"q": "all"}), OWNER, Actor(id="alice"))
    assert conn.requests[-1]["params"]["owner_id"] == "alice"
    assert conn.requests[-1]["params"]["q"] == "all"


def test_email_records_to_outbox() -> None:
    conn = EmailConnector()
    result = conn.execute(
        _resolve("Email", "sendEmail", {"to": "ops@acme.example", "subject": "hi"}),
        None,
        Actor(id="alice"),
    )
    assert result.receipt == {"sent": True, "to": "ops@acme.example"}
    assert conn.outbox[-1]["to"] == "ops@acme.example"
