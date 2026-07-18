# SPDX-License-Identifier: Apache-2.0
"""A tiny, framework-free trace bus (intent → decision → effect) for the UI.

The gateway publishes one event per enforced intent and one per dispatched
effect; the web UI subscribes over a WebSocket to render the live trace. The bus
is thread-safe because the dispatch worker (a background thread) publishes effect
events while the request thread publishes decision events. It keeps a bounded
ring buffer so a UI that connects mid-run can backfill recent activity.

This module imports nothing heavy — the FastAPI/WebSocket adapter lives in
``gateway`` and merely subscribes a callback here.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable, Mapping
from typing import Any

TraceEvent = Mapping[str, Any]
Subscriber = Callable[[TraceEvent], None]


class TraceBus:
    """Synchronous, thread-safe fan-out with a bounded history."""

    def __init__(self, *, history: int = 200) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[Subscriber] = []
        self._history: deque[TraceEvent] = deque(maxlen=history)

    def subscribe(self, callback: Subscriber) -> Callable[[], None]:
        """Register ``callback``; returns an unsubscribe thunk."""
        with self._lock:
            self._subscribers.append(callback)

        def _unsubscribe() -> None:
            with self._lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return _unsubscribe

    def publish(self, event: TraceEvent) -> None:
        with self._lock:
            self._history.append(event)
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback(event)
            except Exception:
                # a slow/broken subscriber must never break enforcement or dispatch
                pass

    def recent(self) -> list[TraceEvent]:
        with self._lock:
            return list(self._history)
