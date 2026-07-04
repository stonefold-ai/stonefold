"""The Accounts-Payable demo over **real Postgres + Redis** (acceptance §G, B2, E1).

Exercises ``build_postgres_bundle`` — the exact backend the docker-compose demo
uses — against testcontainers: scope-injected reads, the staged-then-dispatched
pay, the indirect-injection refusal, approval release, the kill HALT, and audit
replay. Skipped when psycopg / redis / testcontainers / Docker are unavailable.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

import pytest

os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")

pytest.importorskip("psycopg")
pytest.importorskip("redis")
pytest.importorskip("testcontainers.postgres")
pytest.importorskip("testcontainers.redis")

import psycopg  # noqa: E402
import redis as redislib  # noqa: E402
from testcontainers.postgres import PostgresContainer  # noqa: E402
from testcontainers.redis import RedisContainer  # noqa: E402

from stonefold_core import Actor, Decision, KillScope  # noqa: E402
from stonefold_ap_demo.gateway import APBundle, build_postgres_bundle  # noqa: E402
from stonefold_ap_demo.principals import AP_OPERATOR, PAYMENTS_MANAGER  # noqa: E402

pytestmark = pytest.mark.integration

DEMO_NOW = datetime(2026, 6, 28, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def stack() -> Iterator[tuple[PostgresContainer, RedisContainer]]:
    with PostgresContainer("postgres:16-alpine", username="acp", password="acp",
                           dbname="acp") as pg, RedisContainer("redis:7-alpine") as rc:
        yield pg, rc


def _connect(pg: PostgresContainer) -> Any:
    return psycopg.connect(
        host=pg.get_container_host_ip(), port=int(pg.get_exposed_port(5432)),
        user="acp", password="acp", dbname="acp", autocommit=True,
    )


@pytest.fixture
def bundle(stack: tuple[PostgresContainer, RedisContainer]) -> APBundle:
    pg, rc = stack
    conn = _connect(pg)
    # clean the gateway tables (the ledger tables are reset by the reseed below)
    with conn.cursor() as cur:
        for tbl in ("pending_actions", "audit_log", "kill_orders"):
            cur.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE")
    client = redislib.Redis(host=rc.get_container_host_ip(),
                            port=int(rc.get_exposed_port(6379)), decode_responses=True)
    client.flushall()
    return build_postgres_bundle(conn, client, clock=lambda: DEMO_NOW, seed=True)


def _pay(bundle: APBundle, data: dict[str, Any], *, session: str = "pg") -> Any:
    return bundle.submit(actor_id=AP_OPERATOR, resource="Payment", action="pay",
                         data=data, session_id=session, correlation_id=session)


_ACME_800 = {"payeeId": "PE-ACME-SUP", "accountId": "ACME-OPS", "amount": 800.0,
             "currency": "USD", "destinationCountry": "GB", "invoiceId": "INV-1001"}


def _payment_count(bundle: APBundle) -> int:
    rows, _ = bundle.ledger.observe("payment", None, Actor(id="probe"))
    return len(rows)


def test_happy_pays_over_postgres(bundle: APBundle) -> None:
    result = _pay(bundle, dict(_ACME_800))
    assert result.decision is Decision.ALLOW and result.ticket is not None
    assert bundle.drain() == 1
    assert _payment_count(bundle) == 1


def test_observe_scoped_over_postgres(bundle: APBundle) -> None:
    result = bundle.submit(actor_id=AP_OPERATOR, resource="Account", action="read",
                           data={}, session_id="pg")
    assert result.decision is Decision.ALLOW
    assert result.output is not None
    assert {r["id"] for r in result.output} == {"ACME-OPS"}  # rival tenant invisible


def test_b2_scope_denied_over_postgres(bundle: APBundle) -> None:
    result = _pay(bundle, dict(_ACME_800) | {"accountId": "RIVAL-OPS"})
    assert result.decision is Decision.DENY and result.rule == "scope-denied"
    assert _payment_count(bundle) == 0


def test_attack_denied_over_postgres(bundle: APBundle) -> None:
    attack = {"newPayee": "QuickPay Settlements", "iban": "GB91QUICK0000099999",
              "amount": 50_000.0, "currency": "USD", "destinationCountry": "GB",
              "accountId": "ACME-OPS"}
    result = _pay(bundle, attack)
    assert result.decision is Decision.DENY and "precondition" in result.rule
    assert bundle.drain() == 0 and _payment_count(bundle) == 0


def test_approval_release_over_postgres(bundle: APBundle) -> None:
    data = {"payeeId": "PE-GLOBEX", "accountId": "ACME-OPS", "amount": 6_000.0,
            "currency": "USD", "destinationCountry": "US"}
    result = _pay(bundle, data)
    assert result.decision is Decision.HOLD and result.ticket is not None
    assert bundle.drain() == 0
    bundle.approve(result.ticket, PAYMENTS_MANAGER)
    assert bundle.drain() == 1 and _payment_count(bundle) == 1


def test_kill_halts_over_postgres(bundle: APBundle) -> None:
    assert _pay(bundle, dict(_ACME_800), session="live").decision is Decision.ALLOW
    bundle.issue_kill(KillScope.for_session("live"), issued_by="operator")
    assert _pay(bundle, dict(_ACME_800), session="live").decision is Decision.HALT


def test_audit_replay_over_postgres(bundle: APBundle) -> None:
    _pay(bundle, dict(_ACME_800), session="run-x")
    _pay(bundle, dict(_ACME_800) | {"destinationCountry": "KP"}, session="run-x")
    records = bundle.audit_reader.by_correlation("run-x")
    decisions = {r.decision.value for r in records}
    assert "allow" in decisions and "deny" in decisions
