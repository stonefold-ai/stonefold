"""M2 — the counter gates against **real Redis** via testcontainers (DoD).

Proves ``RedisCounterStore`` reproduces the same sliding-window semantics as the
in-memory fake, and that a counter gate driven by Redis enforces C2/C5. Skipped
automatically when redis / testcontainers / Docker are unavailable.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest

# Disable the testcontainers Ryuk reaper: on Docker Desktop / Windows its port
# (8080) often can't be mapped, and our own `with` block already tears the Redis
# container down. Must be set before testcontainers is imported.
os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")

pytest.importorskip("redis")
pytest.importorskip("testcontainers.redis")

import redis as redislib  # noqa: E402
from testcontainers.redis import RedisContainer  # noqa: E402

from stonefold_core.enums import Outcome  # noqa: E402
from stonefold_core.gating import RequestEnv  # noqa: E402
from stonefold_gates.gates import rate, spend_limit  # noqa: E402
from stonefold_store.redis_store import RedisCounterStore  # noqa: E402
from tests.conftest import gate_ctx  # noqa: E402

pytestmark = pytest.mark.integration

T0 = datetime(2026, 6, 28, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def redis_store() -> Iterator["RedisCounterStore"]:
    with RedisContainer("redis:7-alpine") as container:
        client = redislib.Redis(
            host=container.get_container_host_ip(),
            port=int(container.get_exposed_port(6379)),
            decode_responses=True,
        )
        yield RedisCounterStore(client)


def test_redis_hit_sliding_window(redis_store: RedisCounterStore) -> None:
    ts = T0.timestamp()
    assert redis_store.hit("k1", ts, 86400) == 1
    assert redis_store.hit("k1", ts + 3600, 86400) == 2
    assert redis_store.hit("k1", ts + 7200, 86400) == 3
    # a hit a day later prunes the earlier three out of the window
    assert redis_store.hit("k1", ts + 200_000, 86400) == 1


def test_redis_add_sums_in_window(redis_store: RedisCounterStore) -> None:
    ts = T0.timestamp()
    assert redis_store.add("spend1", 10.0, ts, 3600) == pytest.approx(10.0)
    assert redis_store.add("spend1", 7.5, ts + 60, 3600) == pytest.approx(17.5)
    # an hour later the first two amounts have aged out
    assert redis_store.add("spend1", 1.0, ts + 4000, 3600) == pytest.approx(1.0)


def test_c2_rate_gate_over_redis(redis_store: RedisCounterStore) -> None:
    cfg = {"limit": "3/day", "per": "resource.customerId"}

    def attempt(customer: str, when: datetime) -> Outcome:
        env = RequestEnv(now=when, resource={"customerId": customer})
        return rate(cfg, gate_ctx("Payment", "pay", env=env, counters=redis_store)).outcome

    assert attempt("RC", T0) is Outcome.PASS
    assert attempt("RC", T0 + timedelta(hours=1)) is Outcome.PASS
    assert attempt("RC", T0 + timedelta(hours=2)) is Outcome.PASS
    assert attempt("RC", T0 + timedelta(hours=3)) is Outcome.FAIL
    assert attempt("RD", T0 + timedelta(hours=3)) is Outcome.PASS


def test_spend_limit_gate_over_redis(redis_store: RedisCounterStore) -> None:
    def attempt(cost: float, when: datetime) -> Outcome:
        env = RequestEnv(now=when, cost=cost)
        return spend_limit("25/session", gate_ctx("Payment", "pay", env=env, counters=redis_store)).outcome

    assert attempt(10, T0) is Outcome.PASS
    assert attempt(10, T0 + timedelta(seconds=1)) is Outcome.PASS
    assert attempt(10, T0 + timedelta(seconds=2)) is Outcome.FAIL
