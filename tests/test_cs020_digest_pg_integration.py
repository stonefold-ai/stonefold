"""CS-020 — dispatch-time connector digest verification over **real Postgres**.

The dispatch check is the one that guards a *staged* effect: the durable
``pending_actions`` row carries the pinned digest, and the worker refuses to
dispatch through a connector whose loaded artifact no longer matches it (RFC §10,
fail closed). Confirms the refusal is durable — the row settles FAILED with the
mismatch reason and the settle audit lands in the same transaction — while a
matching pin dispatches normally. Skipped when psycopg / testcontainers / Docker
are unavailable.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest

os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")

pytest.importorskip("psycopg")
pytest.importorskip("testcontainers.postgres")

import psycopg  # noqa: E402
from testcontainers.postgres import PostgresContainer  # noqa: E402

from stonefold_core import (  # noqa: E402
    Actor,
    Connectors,
    PendingState,
    RawCall,
    artifact_digest,
    load_registry,
)
from stonefold_core.digest import DIGEST_MISMATCH  # noqa: E402
from stonefold_connectors import InMemoryConnector  # noqa: E402
from stonefold_store import DispatchWorker  # noqa: E402
from stonefold_store.outbox_pg import PostgresOutboxStore, create_schema  # noqa: E402
from tests.conftest import REGISTRY_DIR, load_yaml  # noqa: E402

pytestmark = pytest.mark.integration

BOGUS = "sha256:" + "0" * 64


def _connect(container: PostgresContainer) -> Any:
    return psycopg.connect(
        host=container.get_container_host_ip(),
        port=int(container.get_exposed_port(5432)),
        user="stonefold",
        password="stonefold",
        dbname="stonefold",
        autocommit=True,
    )


@pytest.fixture(scope="module")
def container() -> Iterator[PostgresContainer]:
    with PostgresContainer(
        "postgres:16-alpine", username="stonefold", password="stonefold", dbname="stonefold"
    ) as pg:
        conn = _connect(pg)
        create_schema(conn)
        conn.close()
        yield pg


def _truncate(conn: Any) -> None:
    with conn.cursor() as cur:
        cur.execute("TRUNCATE pending_actions, audit_log")


def _registry_with_digests(digests: dict[str, str]) -> Any:
    data = load_yaml(REGISTRY_DIR / "stonefold-registry.yaml")
    data["connector_digests"] = digests
    return load_registry(data)


def _connector_name(reg: Any) -> str:
    return str(reg.resolve(RawCall(resource="Email", action="sendEmail")).connector)


def test_dispatch_digest_mismatch_fails_closed_pg(container: PostgresContainer) -> None:
    conn = _connect(container)
    _truncate(conn)
    reg = _registry_with_digests({"email": BOGUS})
    cname = _connector_name(reg)
    store = PostgresOutboxStore(conn)
    effect_conn = InMemoryConnector()
    worker = DispatchWorker(store, Connectors({cname: effect_conn}), registry=reg)

    staged = store.stage(
        resolved=reg.resolve(RawCall(resource="Email", action="sendEmail",
                                     data={"to": "x@acme.example"})),
        actor=Actor(id="alice"), session_id="s1", agent="support",
        state=PendingState.PENDING,
    )
    assert store.get(staged.id).state is PendingState.PENDING  # type: ignore[union-attr]

    assert worker.drain() == 1
    row = store.get(staged.id)
    assert row is not None
    assert row.state is PendingState.FAILED
    assert row.reason == DIGEST_MISMATCH
    assert effect_conn.effects == []  # the effect never left

    # the refusal is durable and audited in the settle transaction (invariant 6)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM audit_log WHERE record->>'rule' = %s",
                    (DIGEST_MISMATCH,))
        assert cur.fetchone()[0] == 1
    # a re-drain finds nothing PENDING ⇒ no retry loop
    assert worker.drain() == 0
    conn.close()


def test_dispatch_digest_match_dispatches_pg(container: PostgresContainer) -> None:
    conn = _connect(container)
    _truncate(conn)
    effect_conn = InMemoryConnector()
    reg = _registry_with_digests({"email": artifact_digest(effect_conn)})
    cname = _connector_name(reg)
    store = PostgresOutboxStore(conn)
    worker = DispatchWorker(store, Connectors({cname: effect_conn}), registry=reg)

    staged = store.stage(
        resolved=reg.resolve(RawCall(resource="Email", action="sendEmail",
                                     data={"to": "x@acme.example"})),
        actor=Actor(id="alice"), session_id="s1", agent="support",
        state=PendingState.PENDING,
    )
    assert worker.drain() == 1
    row = store.get(staged.id)
    assert row is not None and row.state is PendingState.DONE
    assert len(effect_conn.effects) == 1
    conn.close()
