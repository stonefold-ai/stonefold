"""acp_store — the stateful backing stores for gates (design §6, §13).

The deterministic gate logic lives in ``acp_gates``; the *counters* those gates
read live here so they can be swapped between an in-memory fake (the M2 unit
suite) and Redis (integration, production) behind one ``CounterStore`` protocol.
Losing the store fails the counter **closed**, never silently-allow (design §13).
"""

from __future__ import annotations

from acp_store.counters import CounterStore, InMemoryCounterStore
from acp_store.dispatch import DispatchWorker
from acp_store.inflight import InFlightCall, InFlightRegistry
from acp_store.kill_cached import CachedKillStore, KillBus
from acp_store.kill_memory import InMemoryKillStore
from acp_store.outbox_memory import InMemoryOutboxStore, build_pending

__all__ = [
    "CounterStore",
    "InMemoryCounterStore",
    "InMemoryOutboxStore",
    "build_pending",
    "DispatchWorker",
    # kill-switch
    "InMemoryKillStore",
    "CachedKillStore",
    "KillBus",
    "InFlightRegistry",
    "InFlightCall",
]
