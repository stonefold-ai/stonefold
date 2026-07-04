"""The fake ledger + the money-moving connector (design §5; CLAUDE.md).

``LedgerConnector`` is the only thing that touches the "bank". It executes
scope-filtered reads (Account/Payment/Payee), stages nothing itself (the pipeline
does), and on **dispatch** of a ``pay`` writes a payment row + emits an effect
event — that is "money leaving the building". It applies the injected scope but
holds **no policy** (CLAUDE.md): every decision was already made upstream.

Two interchangeable backends sit behind one ``LedgerBackend`` protocol: an
in-memory dict store (fast unit tests, the fake-LLM CI path) and a Postgres store
(the docker-compose ledger). The scope predicate is realised *inside* the backend
so a SQL read genuinely carries the injected ``WHERE`` (the "below the model"
property, acceptance B1), while the in-memory backend filters rows with the same
predicate object.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any, Protocol

from stonefold_core.connector import ConnectorResult, ScopeCapability
from stonefold_core.enums import Kind
from stonefold_core.models import Actor, ResolvedAction
from stonefold_core.scope import ScopePredicate

# resource name (registry) -> ledger table
TABLE_FOR: dict[str, str] = {
    "Account": "account",
    "Payment": "payment",
    "Payee": "payee",
    "Invoice": "invoice",
    "LedgerEntry": "ledger_entry",
}

# A trace emitter: the connector calls it once per real effect so the UI/audit can
# show money moving. ``None`` disables tracing.
EffectSink = Callable[[Mapping[str, Any]], None]
Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class LedgerBackend(Protocol):
    """Storage seam: the connector maps action kinds onto these calls."""

    def observe(
        self, table: str, scope: ScopePredicate | None, actor: Actor
    ) -> tuple[list[dict[str, Any]], str]:
        """Return ``(rows, realised_query)`` for a scoped read."""
        ...

    def fetch_account(
        self, account_id: str | None, scope: ScopePredicate | None, actor: Actor
    ) -> dict[str, Any] | None:
        """Resolve the source account under scope (the ``pay`` pre-check target)."""
        ...

    def record_payment(
        self, row: dict[str, Any], idempotency_key: str
    ) -> tuple[dict[str, Any], bool]:
        """Idempotently persist a payment. Returns ``(row, newly_inserted)``."""
        ...

    def record_entry(self, row: dict[str, Any]) -> None: ...

    def transition_invoice(
        self, invoice_id: str | None, to_state: str,
        scope: ScopePredicate | None, actor: Actor,
    ) -> bool: ...


# --------------------------------------------------------------------------- #
# In-memory backend (unit tests + fake-LLM CI path)                            #
# --------------------------------------------------------------------------- #
class InMemoryLedger:
    """A dict-backed ``LedgerBackend`` seeded from ``seed`` data."""

    def __init__(self, tables: Mapping[str, list[dict[str, Any]]] | None = None) -> None:
        self.tables: dict[str, list[dict[str, Any]]] = {
            k: [dict(r) for r in v] for k, v in (tables or {}).items()
        }
        self.tables.setdefault("payment", [])
        self.tables.setdefault("ledger_entry", [])
        self._dispatched: dict[str, dict[str, Any]] = {}

    def observe(
        self, table: str, scope: ScopePredicate | None, actor: Actor
    ) -> tuple[list[dict[str, Any]], str]:
        rows = self.tables.get(table, [])
        if scope is not None:
            rows = [r for r in rows if scope.matches(r, actor)]
            query = f"SELECT * FROM {table} WHERE {scope.name}(actor)"
        else:
            query = f"SELECT * FROM {table}"
        return [dict(r) for r in rows], query

    def fetch_account(
        self, account_id: str | None, scope: ScopePredicate | None, actor: Actor
    ) -> dict[str, Any] | None:
        if account_id is None:
            return None
        for row in self.tables.get("account", []):
            if str(row.get("id")) == str(account_id):
                if scope is None or scope.matches(row, actor):
                    return dict(row)
                return None  # exists, but outside the actor's tenant ⇒ DENY
        return None

    def record_payment(
        self, row: dict[str, Any], idempotency_key: str
    ) -> tuple[dict[str, Any], bool]:
        if idempotency_key in self._dispatched:  # idempotent: never double-send
            return self._dispatched[idempotency_key], False
        stored = dict(row)
        self.tables["payment"].append(stored)
        self._dispatched[idempotency_key] = stored
        return stored, True

    def record_entry(self, row: dict[str, Any]) -> None:
        self.tables["ledger_entry"].append(dict(row))

    def transition_invoice(
        self, invoice_id: str | None, to_state: str,
        scope: ScopePredicate | None, actor: Actor,
    ) -> bool:
        for row in self.tables.get("invoice", []):
            if str(row.get("id")) == str(invoice_id):
                row["status"] = to_state
                return True
        return False

    # inspection helpers for the demo/tests
    def payments(self) -> list[dict[str, Any]]:
        return self.tables.get("payment", [])


# --------------------------------------------------------------------------- #
# Postgres backend (docker-compose ledger)                                     #
# --------------------------------------------------------------------------- #
def _coerce(row: Mapping[str, Any]) -> dict[str, Any]:
    """Make a DB row JSON-friendly (Decimal → float, datetime → isoformat)."""
    import decimal

    out: dict[str, Any] = {}
    for k, v in row.items():
        if isinstance(v, decimal.Decimal):
            out[k] = float(v)
        elif isinstance(v, datetime):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


class PostgresLedger:
    """A psycopg-backed ``LedgerBackend``. ``conn`` is a live, autocommit
    ``psycopg.Connection``; ``psycopg`` is imported lazily by the caller."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def observe(
        self, table: str, scope: ScopePredicate | None, actor: Actor
    ) -> tuple[list[dict[str, Any]], str]:
        from psycopg.rows import dict_row

        where, params = "1=1", {}
        if scope is not None:
            clause, scope_params = scope.sql_where(actor)
            where = f"{where} AND {clause}"
            params.update(scope_params)
        sql = f"SELECT * FROM {table} WHERE {where}"
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            rows = [_coerce(r) for r in cur.fetchall()]
        return rows, sql

    def fetch_account(
        self, account_id: str | None, scope: ScopePredicate | None, actor: Actor
    ) -> dict[str, Any] | None:
        from psycopg.rows import dict_row

        if account_id is None:
            return None
        where, params = "id = %(_id)s", {"_id": account_id}
        if scope is not None:
            clause, scope_params = scope.sql_where(actor)
            where = f"{where} AND {clause}"
            params.update(scope_params)
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(f"SELECT * FROM account WHERE {where}", params)
            row = cur.fetchone()
        return _coerce(row) if row is not None else None

    def record_payment(
        self, row: dict[str, Any], idempotency_key: str
    ) -> tuple[dict[str, Any], bool]:
        from psycopg.rows import dict_row

        cols = ["id", "idempotency_key", "tenant_id", "payee_id", "payee_name",
                "account_id", "amount", "currency", "destination_country", "iban",
                "invoice_id", "status"]
        values = {c: row.get(c) for c in cols}
        values["idempotency_key"] = idempotency_key
        placeholders = ", ".join(f"%({c})s" for c in cols)
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"INSERT INTO payment ({', '.join(cols)}) VALUES ({placeholders}) "
                "ON CONFLICT (idempotency_key) DO NOTHING RETURNING *",
                values,
            )
            inserted = cur.fetchone()
            if inserted is not None:
                return _coerce(inserted), True
            # dup ⇒ a worker retry; return the already-stored row, do not re-emit.
            cur.execute(
                "SELECT * FROM payment WHERE idempotency_key = %s", (idempotency_key,)
            )
            existing = cur.fetchone()
        if existing is not None:
            return _coerce(existing), False
        return dict(row), False

    def record_entry(self, row: dict[str, Any]) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO ledger_entry (tenant_id, payment_id, memo, amount) "
                "VALUES (%(tenant_id)s, %(payment_id)s, %(memo)s, %(amount)s)",
                {k: row.get(k) for k in ("tenant_id", "payment_id", "memo", "amount")},
            )

    def transition_invoice(
        self, invoice_id: str | None, to_state: str,
        scope: ScopePredicate | None, actor: Actor,
    ) -> bool:
        where, params = "id = %(_id)s", {"_id": invoice_id, "_to": to_state}
        if scope is not None:
            clause, scope_params = scope.sql_where(actor)
            where = f"{where} AND {clause}"
            params.update(scope_params)
        with self._conn.cursor() as cur:
            cur.execute(f"UPDATE invoice SET status = %(_to)s WHERE {where}", params)
            return bool((cur.rowcount or 0) > 0)


# --------------------------------------------------------------------------- #
# The connector                                                                #
# --------------------------------------------------------------------------- #
class LedgerConnector:
    """The single adapter to the fake bank. Satisfies ``stonefold_core.Connector``."""

    # CS-018: a payment rail cannot carry the scope predicate into its own
    # transaction — the residual window is declared, and the worker re-resolves
    # the source account under scope immediately before dispatch.
    scope_capability = ScopeCapability.window_declared("payment-rail call")

    def __init__(
        self,
        backend: LedgerBackend,
        *,
        on_effect: EffectSink | None = None,
        clock: Clock = _utcnow,
    ) -> None:
        self._backend = backend
        self._on_effect = on_effect
        self._clock = clock

    def execute(
        self, action: ResolvedAction, scope: ScopePredicate | None, actor: Actor
    ) -> ConnectorResult:
        if action.kind is Kind.OBSERVE:
            table = TABLE_FOR.get(action.resource, action.resource.lower())
            rows, query = self._backend.observe(table, scope, actor)
            return ConnectorResult(kind="rows", rows=rows, query=query)
        if action.kind is Kind.RECORD:
            self._backend.record_entry({
                "tenant_id": actor.claims.get("tenant"),
                "payment_id": action.data.get("paymentId"),
                "memo": action.data.get("memo", action.action),
                "amount": action.data.get("amount"),
            })
            return ConnectorResult(kind="receipt", receipt={"created": True})
        if action.kind is Kind.TRANSITION:
            ok = self._backend.transition_invoice(
                action.data.get("id") or action.data.get("invoiceId"),
                action.data.get("to") or (action.action or ""),
                scope, actor,
            )
            return ConnectorResult(kind="receipt", receipt={"transitioned": ok})
        # An EFFECT never reaches execute (effects are staged → ``dispatch``); be
        # defensive and treat it as a no-op receipt rather than moving money here.
        return ConnectorResult(kind="receipt", receipt={"noop": True})

    def dispatch(
        self, action: ResolvedAction, actor: Actor, idempotency_key: str
    ) -> ConnectorResult:
        data = action.data
        payee_id = data.get("payeeId")
        payment = {
            "id": f"PAY-{uuid.uuid4().hex[:10]}",
            "tenant_id": actor.claims.get("tenant"),
            "payee_id": payee_id,
            "payee_name": data.get("newPayee") or data.get("payeeName") or payee_id,
            "account_id": data.get("accountId") or data.get("account_id"),
            "amount": float(data.get("amount", 0) or 0),
            "currency": data.get("currency", "USD"),
            "destination_country": data.get("destinationCountry"),
            "iban": data.get("iban"),
            "invoice_id": data.get("invoiceId"),
            "status": "sent",
        }
        stored, inserted = self._backend.record_payment(payment, idempotency_key)
        if inserted and self._on_effect is not None:
            self._on_effect({
                "type": "effect", "connector": "ledger-pay",
                "action": "pay", "payment": stored, "key": idempotency_key,
            })
        return ConnectorResult(
            kind="receipt", receipt=stored, handle=idempotency_key,
            # the payment id is the downstream handle an operator/external tool uses
            # to locate and (via refund) compensate this effect — record it (CS-009).
            # (One element here; a fan-out connector would list every record it wrote,
            # e.g. the payment *and* its ledger entry.)
            result_refs=[str(stored["id"])] if stored.get("id") is not None else [],
        )

    def fetch_target(
        self, action: ResolvedAction, scope: ScopePredicate | None, actor: Actor
    ) -> Mapping[str, Any] | None:
        # The pre-resolution scope target for a ``pay`` is the SOURCE account: you
        # may only pay *from* an account in your own tenant (acceptance B2). A new
        # payment has no pre-existing Payment row, so the Account is the thing to
        # authorize against.
        account_id = action.data.get("accountId") or action.data.get("account_id")
        return self._backend.fetch_account(account_id, scope, actor)

    def cancel(self, handle: str) -> None:
        return None


# --------------------------------------------------------------------------- #
# Email stub (registry pins Email→email; payments-ops never allows it, so this  #
# is only here for connector-map completeness / spec's "send-email" stub)       #
# --------------------------------------------------------------------------- #
class EmailStub:
    """A clearly-fake email connector. payments-ops allows no Email action, so any
    attempt is default-denied before this is ever reached — it exists only so the
    connector map covers the registry's ``email`` binding."""

    scope_capability = ScopeCapability.window_declared("smtp stub")

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def execute(
        self, action: ResolvedAction, scope: ScopePredicate | None, actor: Actor
    ) -> ConnectorResult:
        return ConnectorResult(kind="receipt", receipt={"noop": True})

    def dispatch(
        self, action: ResolvedAction, actor: Actor, idempotency_key: str
    ) -> ConnectorResult:
        record = {"action": action.action, "data": dict(action.data), "key": idempotency_key}
        self.sent.append(record)
        return ConnectorResult(kind="receipt", receipt={"sent": True}, handle=idempotency_key)

    def fetch_target(
        self, action: ResolvedAction, scope: ScopePredicate | None, actor: Actor
    ) -> Mapping[str, Any] | None:
        return {"ok": True}

    def cancel(self, handle: str) -> None:
        return None


# --------------------------------------------------------------------------- #
# The new-payee cooling-off precondition (registry: payeeCoolingOffElapsed)     #
# --------------------------------------------------------------------------- #
def payee_cooling_off_elapsed(gctx: Any) -> bool:
    """RFC §7.6 named check. A payee introduced *inline* (the policy only runs
    this gate ``when: "exists data.newPayee"``) has, by definition, not aged the
    required cooling-off window — so the hold always applies. This deliberately
    ignores any agent-supplied ``payeeCoolingOffElapsed`` flag: an attacker must
    not be able to self-clear the check (the default ``_run_named_check`` fallback
    would otherwise honour ``data.payeeCoolingOffElapsed: true``). A real
    deployment records each payee's first-seen timestamp and compares it to now.
    """
    return False
