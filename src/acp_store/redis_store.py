"""Redis-backed sliding-window counters (design §6, §13).

Each counter key is a Redis **sorted set** whose members are unique event ids
scored by timestamp; ``hit``/``add`` prune the window with ``ZREMRANGEBYSCORE``
and read back the surviving members. For ``add`` (spend) the per-event amount is
kept in a companion hash so the window sum is exact. All operations for one call
run in a single ``MULTI``/``EXEC`` pipeline.

If Redis is unreachable the underlying client raises and the gate fails **closed**
(design §13) — we never catch-and-return-zero here.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from redis import Redis


class RedisCounterStore:
    """A ``CounterStore`` whose state lives in Redis (shared across gateways)."""

    def __init__(self, client: "Redis", *, namespace: str = "acp:ctr") -> None:
        self._r = client
        self._ns = namespace

    def _zkey(self, key: str) -> str:
        return f"{self._ns}:z:{key}"

    def _hkey(self, key: str) -> str:
        return f"{self._ns}:h:{key}"

    def hit(self, key: str, now: float, window_s: float) -> int:
        zkey = self._zkey(key)
        member = uuid.uuid4().hex
        cutoff = now - window_s
        pipe = self._r.pipeline(transaction=True)
        pipe.zremrangebyscore(zkey, "-inf", cutoff)
        pipe.zadd(zkey, {member: now})
        pipe.zcard(zkey)
        # keep the set from leaking if a key falls idle
        pipe.expire(zkey, int(window_s) + 1)
        results: list[Any] = pipe.execute()
        return int(results[2])

    def add(self, key: str, amount: float, now: float, window_s: float) -> float:
        zkey, hkey = self._zkey(key), self._hkey(key)
        member = uuid.uuid4().hex
        cutoff = now - window_s
        ttl = int(window_s) + 1

        # 1) prune expired members from BOTH the index and the amount hash
        expired = [str(m) for m in self._r.zrangebyscore(zkey, "-inf", cutoff)]
        prune = self._r.pipeline(transaction=True)
        if expired:
            prune.zrem(zkey, *expired)
            prune.hdel(hkey, *expired)
        prune.zadd(zkey, {member: now})
        prune.hset(hkey, member, amount)
        prune.expire(zkey, ttl)
        prune.expire(hkey, ttl)
        prune.execute()

        # 2) sum the surviving amounts
        members = [str(m) for m in self._r.zrange(zkey, 0, -1)]
        if not members:
            return 0.0
        amounts = self._r.hmget(hkey, members)
        return sum(float(a) for a in amounts if a is not None)
