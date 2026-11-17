"""Persistent uncertainty and abandoned-claim recovery; isolated real Postgres."""
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import json
import subprocess
import sys
from typing import Any

import pytest

from tests.test_m4_pg_integration import container, _connect, _resolve  # noqa: F401
from tests.test_execution_recovery import EffectThenTimeout
from stonefold_core import Actor, Connectors, PendingState
from stonefold_store import DispatchWorker
from stonefold_store.audit_pg import PostgresAuditSink
from stonefold_store.outbox_pg import PostgresOutboxStore

pytestmark = pytest.mark.integration


def stage(store: PostgresOutboxStore, correlation: str) -> Any:
    return store.stage(resolved=_resolve("Email", "sendEmail"), actor=Actor(id="alice"),
                       session_id="s", agent="support", state=PendingState.PENDING,
                       correlation_id=correlation)


def test_timeout_persists_unknown_despite_effect(container: Any) -> None:
    with _connect(container) as conn:
        store = PostgresOutboxStore(conn)
        row = stage(store, "timeout")
        effect = EffectThenTimeout()
        assert DispatchWorker(store, Connectors({"email": effect})).drain() == 1
    with _connect(container) as conn:
        observed = PostgresOutboxStore(conn).get(row.id)
        assert observed is not None and observed.state is PendingState.OUTCOME_UNKNOWN
        assert len(effect.effects) == 1
        assert [r.outcome for r in PostgresAuditSink(conn).by_correlation("timeout")] == ["unknown"]


def test_reconstruction_recovers_once_and_audit_failure_rolls_back(container: Any) -> None:
    with _connect(container) as conn:
        store = PostgresOutboxStore(conn)
        row = stage(store, "restart")
    # Abrupt process exit after the committed claim; no finally/worker cleanup.
    child = subprocess.run(
        [sys.executable, "-c", """
import json, os, sys, psycopg
from stonefold_store.outbox_pg import PostgresOutboxStore
conn = psycopg.connect(**json.load(sys.stdin))
row = PostgresOutboxStore(conn).claim_next_pending()
print(row.id, flush=True)
os._exit(0)
"""], input=json.dumps({"host": container.get_container_host_ip(),
        "port": int(container.get_exposed_port(5432)), "user": "stonefold",
        "password": "stonefold", "dbname": "stonefold", "autocommit": True}),
        capture_output=True, text=True, timeout=30, check=True,
    )
    assert child.stdout.strip() == row.id
    cutoff = datetime.now(timezone.utc) + timedelta(minutes=6)

    class BrokenAudit(PostgresOutboxStore):
        def _write_audit(self, cur: Any, audit: Any) -> None:
            raise RuntimeError("audit unavailable")

    with _connect(container) as conn:
        with pytest.raises(RuntimeError, match="audit unavailable"):
            BrokenAudit(conn).recover_dispatching(cutoff)
    with _connect(container) as conn:
        store = PostgresOutboxStore(conn)
        observed = store.get(row.id)
        assert observed is not None and observed.state is PendingState.DISPATCHING
    def sweep() -> int:
        with _connect(container) as conn:
            return PostgresOutboxStore(conn).recover_dispatching(cutoff)

    with ThreadPoolExecutor(max_workers=2) as pool:
        recovered = list(pool.map(lambda _: sweep(), range(2)))
    assert sum(recovered) == 1
    with _connect(container) as conn:
        store = PostgresOutboxStore(conn)
        assert store.recover_dispatching(cutoff) == 0
        observed = store.get(row.id)
        assert observed is not None and observed.state is PendingState.OUTCOME_UNKNOWN
        assert len(PostgresAuditSink(conn).by_correlation("restart")) == 1
