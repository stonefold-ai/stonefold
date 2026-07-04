"""Postgres ``KillStore`` (design §8.2, §8.4). The durable ``kill_orders`` table —
so a kill survives a gateway restart — and the authoritative read the dispatch
worker performs **inside** its ``FOR UPDATE`` transaction (design §8.3 point 3).

A dedicated ``kill_epoch_seq`` advances on every mutation (issue *and* lift), so a
``CachedKillStore`` wrapping this can detect a missed invalidation and reload
(design §8.9). ``psycopg`` is imported lazily so importing this module never
requires it.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from stonefold_core.kill import KillOrder, KillScope, KillTarget, order_matches

_SCHEMA = """
CREATE SEQUENCE IF NOT EXISTS kill_epoch_seq;
CREATE TABLE IF NOT EXISTS kill_orders (
    id          text PRIMARY KEY,
    scope       jsonb NOT NULL,
    predicate   text,
    issued_by   text NOT NULL,
    issued_at   timestamptz NOT NULL,
    lifted_at   timestamptz,
    epoch       bigint NOT NULL
);
CREATE INDEX IF NOT EXISTS kill_orders_active ON kill_orders (lifted_at);
"""


def create_kill_schema(conn: Any) -> None:
    with conn.cursor() as cur:
        cur.execute(_SCHEMA)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _row_to_order(row: tuple[Any, ...]) -> KillOrder:
    oid, scope, predicate, issued_by, issued_at, lifted_at, epoch = row
    scope_data = scope if isinstance(scope, dict) else json.loads(scope)
    return KillOrder(
        id=oid,
        scope=KillScope.model_validate(scope_data),
        predicate=predicate,
        issued_by=issued_by,
        issued_at=issued_at,
        lifted_at=lifted_at,
        epoch=epoch,
    )


_COLUMNS = "id, scope, predicate, issued_by, issued_at, lifted_at, epoch"


class PostgresKillStore:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def issue(
        self, scope: KillScope, *, issued_by: str, predicate: str | None = None
    ) -> KillOrder:
        from psycopg.types.json import Jsonb

        order = KillOrder(
            id=f"kill_{uuid.uuid4().hex[:12]}",
            scope=scope,
            predicate=predicate,
            issued_by=issued_by,
            issued_at=_now(),
        )
        with self._conn.transaction(), self._conn.cursor() as cur:
            cur.execute("SELECT nextval('kill_epoch_seq')")
            epoch = int(cur.fetchone()[0])
            cur.execute(
                """INSERT INTO kill_orders
                   (id, scope, predicate, issued_by, issued_at, epoch)
                   VALUES (%(id)s, %(scope)s, %(predicate)s, %(issued_by)s,
                           %(issued_at)s, %(epoch)s)""",
                {
                    "id": order.id,
                    "scope": Jsonb(order.scope.model_dump(mode="json")),
                    "predicate": order.predicate,
                    "issued_by": order.issued_by,
                    "issued_at": order.issued_at,
                    "epoch": epoch,
                },
            )
        return order.model_copy(update={"epoch": epoch})

    def lift(self, order_id: str) -> KillOrder:
        with self._conn.transaction(), self._conn.cursor() as cur:
            cur.execute("SELECT nextval('kill_epoch_seq')")
            epoch = int(cur.fetchone()[0])
            cur.execute(
                """UPDATE kill_orders SET lifted_at = now(), epoch = %(epoch)s
                   WHERE id = %(id)s""",
                {"epoch": epoch, "id": order_id},
            )
            cur.execute(f"SELECT {_COLUMNS} FROM kill_orders WHERE id = %s", (order_id,))
            row = cur.fetchone()
        if row is None:
            raise KeyError(order_id)
        return _row_to_order(row)

    def active_orders(self) -> tuple[KillOrder, ...]:
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT {_COLUMNS} FROM kill_orders WHERE lifted_at IS NULL ORDER BY epoch"
            )
            rows = cur.fetchall()
        return tuple(_row_to_order(r) for r in rows)

    def matches(self, target: KillTarget) -> KillOrder | None:
        for order in self.active_orders():
            if order_matches(order, target):
                return order
        return None

    def epoch(self) -> int:
        with self._conn.cursor() as cur:
            cur.execute("SELECT coalesce(max(epoch), 0) FROM kill_orders")
            return int(cur.fetchone()[0])
