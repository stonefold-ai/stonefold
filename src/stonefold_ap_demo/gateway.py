"""Assemble the full Stonefold enforcement stack over ``examples/payments-ops.stele.yaml``.

This is the *real product* wiring — registry → compiled policy → gate engine →
scope resolver → ledger connector → outbox → kill store — behind the ``Gateway``
chokepoint, pointed at the **unmodified** shipped payments policy. Two flavours:

* ``build_inmemory_bundle`` — everything in-process (fast unit tests, fake-LLM CI).
* ``build_postgres_bundle`` — Postgres outbox/kill/audit + Redis counters (the
  docker-compose demo; the kill no-race demo needs real ``SELECT … FOR UPDATE``).

``APBundle.submit`` is the single Python entry point the agent's gated tool and the
HTTP transport both call: it resolves identity from the directory (never the body,
invariant 3), runs ``enforce`` once, and publishes a trace event for the UI.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml

from stonefold_core import (
    Actor,
    AuditRecord,
    CompiledPolicy,
    Connectors,
    Decision,
    EvalResult,
    FreshnessConfig,
    KillOrder,
    KillScope,
    PendingAction,
    PendingState,
    RawCall,
    RequestEnv,
    Session,
    load_policy,
    load_registry,
)
from stonefold_core.audit import AuditSink, InMemoryAuditSink, build_record
from stonefold_core.kill import KillStore
from stonefold_core.outbox import OutboxStore
from stonefold_core.scope import make_scope_resolver
from stonefold_connectors import InMemoryConnector
from stonefold_gates.content import default_hooks
from stonefold_gates.engine import DefaultGateEngine, make_dispatch_revalidator
from stonefold_gateway.kill_service import KillService
from stonefold_gateway.transport import Gateway
from stonefold_store import (
    CachedKillStore,
    DispatchWorker,
    InFlightRegistry,
    InMemoryCounterStore,
    InMemoryKillStore,
    InMemoryOutboxStore,
    KillBus,
)
from stonefold_store.counters import CounterStore

from stonefold_ap_demo import DEMO_AGENT
from stonefold_ap_demo.ledger import (
    Clock,
    EmailStub,
    InMemoryLedger,
    LedgerBackend,
    LedgerConnector,
    _utcnow,
    payee_cooling_off_elapsed,
)
from stonefold_ap_demo.principals import AP_OPERATOR, PrincipalDirectory, default_directory
from stonefold_ap_demo.seed import ACCOUNTS, INBOX, PAYEES
from stonefold_ap_demo.trace import TraceBus

_ROOT = Path(__file__).resolve().parents[2]
_REGISTRY = _ROOT / "registry" / "stonefold-registry.yaml"
_SCHEMA = _ROOT / "schema" / "stele.schema.json"
PAYMENTS_POLICY = _ROOT / "examples" / "payments-ops.stele.yaml"


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data: dict[str, Any] = yaml.safe_load(fh)
    return data


class AuditReader(Protocol):
    """The subset of an audit sink the demo/UI reads back for replay."""

    def all_records(self) -> list[AuditRecord]: ...
    def by_correlation(self, correlation_id: str) -> list[AuditRecord]: ...


class _InMemoryAuditReader:
    """Adapts ``InMemoryAuditSink`` (which exposes ``.records`` + ``by_correlation``)
    to the ``AuditReader`` protocol the UI/demo reads through."""

    def __init__(self, sink: InMemoryAuditSink) -> None:
        self._sink = sink

    def all_records(self) -> list[AuditRecord]:
        return list(self._sink.records)

    def by_correlation(self, correlation_id: str) -> list[AuditRecord]:
        return self._sink.by_correlation(correlation_id)


def _seed_inmemory_ledger() -> InMemoryLedger:
    return InMemoryLedger({
        "account": [dict(a) for a in ACCOUNTS],
        "payee": [dict(p) for p in PAYEES],
        "invoice": [
            {"id": inv["id"], "tenant_id": "acme-treasury", "vendor": inv["vendor"],
             "payee_id": inv.get("payee_id"), "amount": inv["amount"],
             "currency": inv["currency"], "account_id": inv["account_id"],
             "destination_country": inv["destination_country"], "status": "sent",
             "body": inv["body"]}
            for inv in INBOX
        ],
    })


@dataclass
class APBundle:
    """Everything needed to drive and inspect one Accounts-Payable gateway."""

    gateway: Gateway
    policy: CompiledPolicy
    audit: AuditSink
    audit_reader: AuditReader
    outbox: OutboxStore
    kill: KillStore
    kill_service: KillService
    worker: DispatchWorker
    inflight: InFlightRegistry
    trace: TraceBus
    ledger: LedgerBackend
    counters: CounterStore
    directory: PrincipalDirectory
    clock: Clock
    known_payee_ids: frozenset[str] = frozenset()

    # One reentrant lock serialises every access to the shared DB connection.
    # The demo runs the gateway on a single psycopg connection (request path,
    # background worker, and the control-plane endpoints all share it); psycopg
    # connections are not safe for concurrent transactions, so all DB-touching
    # bundle methods and the worker loop acquire this lock. (A production gateway
    # uses a connection pool instead — out of scope for the demo.)
    _lock: "threading.RLock" = field(default_factory=threading.RLock)
    _worker_thread: threading.Thread | None = None
    _worker_stop: threading.Event | None = None

    def _enrich(self, resource: str, action: str | None,
                data: dict[str, Any]) -> dict[str, Any]:
        """Gateway-side enrichment from the authoritative ledger: a ``pay`` to a
        payee that is NOT already on file is a *new* payee, so flag it as such for
        the policy's new-payee cooling-off gate (``when: "exists data.newPayee"``).
        This derives the fact from the gateway's own payee list — not from the
        agent — so the cooling-off hold cannot be evaded by however the agent
        happens to name the recipient. STONEFOLD-AMBIGUITY (RFC §7): the policy keys on
        ``data.newPayee``; a real deployment resolves new-payee status from the
        vendor master here.
        """
        if resource == "Payment" and action == "pay" and not data.get("newPayee"):
            payee_id = data.get("payeeId")
            if payee_id is None or str(payee_id) not in self.known_payee_ids:
                data["newPayee"] = str(payee_id or data.get("payeeName") or "unknown-payee")
        return data

    # --- the single chokepoint --------------------------------------------- #
    def submit(
        self,
        *,
        actor_id: str,
        resource: str,
        action: str | None,
        data: dict[str, Any] | None = None,
        session_id: str,
        correlation_id: str | None = None,
    ) -> EvalResult:
        """Resolve identity from the directory and enforce one intent. Identity is
        the authenticated ``actor_id`` (never the body); an unknown principal is an
        audited structural DENY."""
        session = Session(id=session_id, correlation_id=correlation_id or session_id)
        data = self._enrich(resource, action, dict(data or {}))
        actor = self.directory.actor_for(actor_id)
        with self._lock:
            if actor is None:
                result = self.gateway.refuse(
                    reason="unknown-principal", resource=resource, action=action,
                    data=data, actor=Actor(id=actor_id), session=session,
                )
            else:
                result = self.gateway.submit(
                    resource=resource, action=action, data=data,
                    actor=actor, session=session,
                )
        self.trace.publish({
            "type": "decision",
            "actor": actor_id,
            "resource": resource,
            "action": action,
            "data": data,
            "decision": result.decision.value,
            "rule": result.rule,
            "ticket": result.ticket,
            "scopeApplied": list(result.scope_applied),
            "correlationId": session.correlation_id,
        })
        return result

    def drain(self) -> int:
        """Dispatch every staged (allowed) effect now — money for permitted pays
        actually leaves; refused ones never staged, so they never can."""
        with self._lock:
            return self.worker.drain()

    # --- approvals (the inbox the UI/operator acts on) --------------------- #
    def pending_approvals(self) -> list[PendingAction]:
        with self._lock:
            return self.outbox.list_by_state(PendingState.PENDING_APPROVAL)

    def approve(self, action_id: str, approver_id: str) -> PendingAction:
        with self._lock:
            return self.outbox.approve(action_id, approver_id)

    def reject(self, action_id: str, approver_id: str) -> PendingAction:
        with self._lock:
            row = self.outbox.reject(action_id, approver_id)
            # Make the rejection a visible terminal outcome: audit it (DENY) so it
            # shows in the log, parallel to an approved payment's settle record.
            result = EvalResult(decision=Decision.DENY, rule=f"rejected by {approver_id}",
                                ticket=row.id, gates=row.gates)
            self.audit.write(build_record(
                agent=row.agent, actor=row.actor,
                session=Session(id=row.session_id, correlation_id=row.correlation_id),
                call=RawCall(resource=row.resolved.resource, action=row.resolved.action,
                             data=dict(row.resolved.data)),
                resolved=row.resolved, result=result, outcome="not_executed",
            ))
        # emit a trace event so the live trace + audit panel update immediately
        self.trace.publish({
            "type": "decision", "actor": row.actor.id,
            "resource": row.resolved.resource, "action": row.resolved.action,
            "data": dict(row.resolved.data), "decision": "deny",
            "rule": f"rejected by {approver_id}", "ticket": row.id,
            "correlationId": row.correlation_id,
        })
        return row

    # --- kill switch -------------------------------------------------------- #
    def issue_kill(
        self, scope: KillScope, *, issued_by: str, predicate: str | None = None
    ) -> KillOrder:
        with self._lock:
            return self.kill_service.issue(scope, issued_by=issued_by, predicate=predicate)

    def lift_kill(self, order_id: str, *, lifted_by: str) -> KillOrder:
        with self._lock:
            return self.kill_service.lift(order_id, lifted_by=lifted_by)

    def active_kills(self) -> tuple[KillOrder, ...]:
        with self._lock:
            return self.kill_service.active()

    # --- audit (replay / log view) ----------------------------------------- #
    def audit_records(self) -> list[AuditRecord]:
        with self._lock:
            return self.audit_reader.all_records()

    def audit_by_correlation(self, correlation_id: str) -> list[AuditRecord]:
        with self._lock:
            return self.audit_reader.by_correlation(correlation_id)

    # --- background dispatch loop (the live service) ----------------------- #
    def start_worker(self, *, interval_s: float = 0.1) -> None:
        if self._worker_thread is not None:
            return
        stop = threading.Event()

        def _loop() -> None:
            while not stop.is_set():
                handled = False
                try:
                    with self._lock:  # serialise with the request path's DB access
                        handled = self.worker.run_once()
                except Exception:
                    handled = False
                if not handled:
                    time.sleep(interval_s)

        thread = threading.Thread(target=_loop, name="ap-dispatch-worker", daemon=True)
        self._worker_stop = stop
        self._worker_thread = thread
        thread.start()

    def stop_worker(self) -> None:
        if self._worker_stop is not None:
            self._worker_stop.set()
        if self._worker_thread is not None:
            self._worker_thread.join(timeout=2.0)
        self._worker_thread = None
        self._worker_stop = None


def _build_common(
    *,
    ledger: LedgerBackend,
    audit: AuditSink,
    audit_reader: AuditReader,
    outbox: OutboxStore,
    kill: KillStore,
    counters: CounterStore,
    trace: TraceBus,
    clock: Clock,
    directory: PrincipalDirectory,
    policy_path: Path = PAYMENTS_POLICY,
) -> APBundle:
    # ``policy_path`` defaults to the shipped payments policy; the benchmark harness
    # (docs/15) passes an ablation-rung policy over the same registry/domain to vary
    # only the enforcement strength. Every existing caller uses the default.
    registry = load_registry(_load_yaml(_REGISTRY))
    with _SCHEMA.open("r", encoding="utf-8") as fh:
        schema = json.load(fh)
    policy = load_policy(_load_yaml(policy_path), registry, schema=schema)

    connectors = Connectors({
        "sql": LedgerConnector(ledger, on_effect=trace.publish, clock=clock),
        "email": EmailStub(),
        "in_memory": InMemoryConnector(),
    })

    gates = DefaultGateEngine(
        registry,
        counters=counters,
        hooks=default_hooks(),
        preconditions={"payeeCoolingOffElapsed": payee_cooling_off_elapsed},
    )

    def _env_factory(call: RawCall) -> RequestEnv:
        # Per-request env: the live wall clock (time-based gates) + the resolved
        # ``resource`` attributes a gate references. The ``rate`` gate keys on
        # ``resource.payeeId``; populate it so the gate has a partition key (an
        # absent key would fail the gate closed). STONEFOLD-AMBIGUITY (RFC §7): the
        # authoritative payee should come from the ledger; for the demo the
        # partition key falls back to the intended payee in the call.
        data = call.data
        payee = data.get("payeeId") or data.get("newPayee") or "unknown"
        return RequestEnv(now=clock(), resource={"payeeId": str(payee)})

    inflight = InFlightRegistry()
    scopes = make_scope_resolver(policy)
    gateway = Gateway(
        registry=registry,
        audit=audit,
        policy=policy,
        gates=gates,
        scopes=scopes,
        connectors=connectors,
        outbox=outbox,
        kill=kill,
        env_factory=_env_factory,
        # v0.4 CS-017: bound how stale a staged payment decision may get — a
        # payee sanctioned or an approval granted long ago is caught at claim.
        freshness=FreshnessConfig(),
    )
    # v0.4 wiring: the worker's clock is the same injected demo clock the
    # decisions use; it re-runs volatile gates inside the claim (CS-017) and
    # re-asserts scope at dispatch (CS-018 — the ledger connector declares a
    # residual window, so the target account is re-resolved pre-dispatch).
    worker = DispatchWorker(
        outbox, connectors, registry=registry, kill=kill, inflight=inflight,
        clock=clock,
        revalidate=make_dispatch_revalidator(gates, policy),
        scopes=scopes,
    )
    kill_service = KillService(kill, audit=audit, inflight=inflight)

    return APBundle(
        gateway=gateway, policy=policy, audit=audit, audit_reader=audit_reader,
        outbox=outbox, kill=kill, kill_service=kill_service, worker=worker,
        inflight=inflight, trace=trace, ledger=ledger, counters=counters,
        directory=directory, clock=clock,
        known_payee_ids=frozenset(str(p["id"]) for p in PAYEES),
    )


def build_inmemory_bundle(
    *,
    clock: Clock = _utcnow,
    directory: PrincipalDirectory | None = None,
    policy_path: Path = PAYMENTS_POLICY,
) -> APBundle:
    """Fully in-process bundle (no Docker, no key) — the fast test/CI path.

    ``policy_path`` defaults to the shipped payments policy; the benchmark harness
    overrides it to swap in an ablation-rung policy over the same domain.
    """
    audit = InMemoryAuditSink()
    trace = TraceBus()
    return _build_common(
        ledger=_seed_inmemory_ledger(),
        audit=audit,
        audit_reader=_InMemoryAuditReader(audit),
        outbox=InMemoryOutboxStore(audit=audit),
        kill=InMemoryKillStore(),
        counters=InMemoryCounterStore(),
        trace=trace,
        clock=clock,
        directory=directory or default_directory(),
        policy_path=policy_path,
    )


def build_postgres_bundle(
    conn: Any,
    redis_client: Any,
    *,
    clock: Clock = _utcnow,
    directory: PrincipalDirectory | None = None,
    seed: bool = True,
) -> APBundle:
    """Postgres outbox/kill/audit + Redis counters (the docker-compose demo).

    ``conn`` is a live autocommit ``psycopg.Connection``; ``redis_client`` a
    ``redis.Redis``. Creates every schema (idempotent) and optionally seeds the
    fake ledger.
    """
    from stonefold_store.audit_pg import PostgresAuditSink, create_audit_schema
    from stonefold_store.kill_pg import PostgresKillStore, create_kill_schema
    from stonefold_store.outbox_pg import PostgresOutboxStore, create_schema
    from stonefold_store.redis_store import RedisCounterStore

    from stonefold_ap_demo.ledger import PostgresLedger
    from stonefold_ap_demo.seed import LEDGER_DDL, ledger_seed_sql

    create_schema(conn)
    create_audit_schema(conn)
    create_kill_schema(conn)
    with conn.cursor() as cur:
        cur.execute(LEDGER_DDL)
    if seed:
        with conn.cursor() as cur:
            cur.execute(ledger_seed_sql())

    audit = PostgresAuditSink(conn)
    kill = CachedKillStore(PostgresKillStore(conn), bus=KillBus())
    trace = TraceBus()
    return _build_common(
        ledger=PostgresLedger(conn),
        audit=audit,
        audit_reader=audit,
        outbox=PostgresOutboxStore(conn),
        kill=kill,
        counters=RedisCounterStore(redis_client),
        trace=trace,
        clock=clock,
        directory=directory or default_directory(),
    )
