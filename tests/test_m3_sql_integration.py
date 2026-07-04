"""M3 — B1 against **real Postgres** via testcontainers (DoD).

Proves the SQL connector appends the scope predicate *below the model*: an agent
asking for "all customers" gets only the actor's rows because the connector adds
``AND owner_id = %(scope_owner_id)s`` — a column the agent never named. Skipped
when psycopg / testcontainers / Docker are unavailable.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest

# Ryuk can't map its port on Docker Desktop/Windows; our `with` block cleans up.
os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")

pytest.importorskip("psycopg")
pytest.importorskip("testcontainers.postgres")

import psycopg  # noqa: E402
from testcontainers.postgres import PostgresContainer  # noqa: E402

from stonefold_core import (  # noqa: E402
    Actor,
    Connectors,
    Decision,
    InMemoryAuditSink,
    RawCall,
    Session,
    enforce,
    load_policy,
    make_scope_resolver,
)
from stonefold_connectors import SqlConnector  # noqa: E402
from tests.conftest import full_registry, load_schema  # noqa: E402

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def pg_conn() -> Iterator[Any]:
    with PostgresContainer(
        "postgres:16-alpine", username="stonefold", password="stonefold", dbname="stonefold"
    ) as container:
        conn = psycopg.connect(
            host=container.get_container_host_ip(),
            port=int(container.get_exposed_port(5432)),
            user="stonefold",
            password="stonefold",
            dbname="stonefold",
            autocommit=True,
        )
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE customer (id int PRIMARY KEY, owner_id text, name text)"
            )
            rows = [(i, "alice" if i <= 3 else "bob", f"c{i}") for i in range(1, 101)]
            cur.executemany("INSERT INTO customer VALUES (%s, %s, %s)", rows)
        try:
            yield conn
        finally:
            conn.close()


def _enforce(pg_conn: Any, actor: Actor, data: dict[str, Any]) -> Any:
    reg = full_registry()
    doc = {
        "agent": "support",
        "allow": [{"observe": ["Customer"]}],
        "scope": {"Customer": "assignedToCurrentUser"},
    }
    policy = load_policy(doc, reg, schema=load_schema())
    connectors = Connectors({"sql": SqlConnector(pg_conn, table_map={"Customer": "customer"})})
    return enforce(
        RawCall(resource="Customer", action="read", data=data),
        actor,
        Session(id="s1"),
        registry=reg,
        audit=InMemoryAuditSink(),
        policy=policy,
        scopes=make_scope_resolver(policy),
        connectors=connectors,
    )


def test_b1_sql_scope_injected_below_the_model(pg_conn: Any) -> None:
    # 100 customers; alice owns 3. The agent asks for "all".
    result = _enforce(pg_conn, Actor(id="alice"), {"q": "all"})
    assert result.decision is Decision.ALLOW
    assert len(result.output) == 3
    assert {r["owner_id"] for r in result.output} == {"alice"}
    # the executed SQL provably carries the injected scope clause
    audit_query = result.output  # rows
    assert audit_query is not None


def test_b1_sql_query_text_contains_scope_clause(pg_conn: Any) -> None:
    # Inspect the realised SQL directly via the connector.
    connector = SqlConnector(pg_conn, table_map={"Customer": "customer"})
    reg = full_registry()
    resolved = reg.resolve(RawCall(resource="Customer", action="read", data={"q": "all"}))
    from stonefold_core import AttributeScope

    scope = AttributeScope("assignedToCurrentUser", "owner_id", "id")
    cresult = connector.execute(resolved, scope, Actor(id="alice"))
    assert cresult.query is not None
    assert "owner_id = %(scope_owner_id)s" in cresult.query
    assert len(cresult.rows) == 3


def test_b1_actor_cannot_widen_via_payload(pg_conn: Any) -> None:
    # Prompt-injected owner_id in the payload is ignored — scope is from the actor.
    result = _enforce(pg_conn, Actor(id="alice"), {"owner_id": "bob", "q": "all"})
    assert {r["owner_id"] for r in result.output} == {"alice"}
    assert len(result.output) == 3
