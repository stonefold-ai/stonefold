# SPDX-License-Identifier: Apache-2.0
"""Email connector (design §5) — a representative ``effect``. A stub that records
the message to an in-memory outbox and returns a receipt. Effects are *staged*
via the outbox in M4; here the connector only models the send for non-staged use.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from stonefold_core.connector import ConnectorResult, ScopeCapability
from stonefold_core.models import Actor, ResolvedAction
from stonefold_core.scope import ScopePredicate


class EmailConnector:
    # CS-018: SMTP accepts once and cannot re-assert scope at commit — the
    # residual window is declared rather than hidden (B5).
    scope_capability = ScopeCapability.window_declared("smtp accept")

    def __init__(self) -> None:
        self.outbox: list[dict[str, Any]] = []
        self._dispatched: dict[str, ConnectorResult] = {}

    def execute(
        self, action: ResolvedAction, scope: ScopePredicate | None, actor: Actor
    ) -> ConnectorResult:
        message = {
            "to": action.data.get("to"),
            "subject": action.data.get("subject"),
            "body": action.data.get("body"),
        }
        self.outbox.append(message)
        return ConnectorResult(kind="receipt", receipt={"sent": True, "to": message["to"]})

    def dispatch(
        self, action: ResolvedAction, actor: Actor, idempotency_key: str
    ) -> ConnectorResult:
        # SMTP accepts once: dedupe on the idempotency key (design §9).
        if idempotency_key in self._dispatched:
            return self._dispatched[idempotency_key]
        result = self.execute(action, None, actor)
        self._dispatched[idempotency_key] = result
        return result

    def fetch_target(
        self, action: ResolvedAction, scope: ScopePredicate | None, actor: Actor
    ) -> Mapping[str, Any] | None:
        # Email effects carry no scoped target in the examples.
        return dict(action.data)

    def cancel(self, handle: str) -> None:
        return None
