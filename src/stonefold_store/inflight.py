"""The in-flight connector-call registry (design §8.5).

After a row moves to ``DISPATCHING`` the worker hands the effect to a connector;
that call may run for a while (an HTTP request, a job submission). The gateway
keeps each such call here, keyed by its cancellation **handle**, so a kill can
invoke the connector's ``cancel`` and abort what is still abortable. Whether the
external world honours it is connector-dependent (design §8.5): a cancellable
call aborts; a point-of-no-return call cannot be reversed (the worker then relies
on the declared compensation, design §9).

Thread-safe: the worker registers/unregisters from its dispatch thread while an
operator kill cancels from another.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

from stonefold_core.connector import Connector
from stonefold_core.kill import KillTarget


@dataclass(frozen=True)
class InFlightCall:
    """One connector call currently in flight."""

    handle: str
    connector: Connector
    target: KillTarget
    action_id: str


class InFlightRegistry:
    def __init__(self) -> None:
        self._calls: dict[str, InFlightCall] = {}
        self._lock = threading.Lock()

    def register(self, call: InFlightCall) -> None:
        with self._lock:
            self._calls[call.handle] = call

    def unregister(self, handle: str) -> None:
        with self._lock:
            self._calls.pop(handle, None)

    def cancel_matching(self, predicate: Callable[[InFlightCall], bool]) -> list[str]:
        """Cancel every in-flight call whose entry satisfies ``predicate`` (e.g.
        ``order_matches(order, call.target)``). Returns the handles cancelled."""
        with self._lock:
            doomed = [c for c in self._calls.values() if predicate(c)]
        cancelled: list[str] = []
        for call in doomed:
            call.connector.cancel(call.handle)
            cancelled.append(call.handle)
        return cancelled
