"""HTTP/REST connector (design §5). The scope predicate becomes a **mandatory
query parameter** the connector injects server-side — the agent never supplies it.

No real network: an optional ``sender`` callable lets tests stand in for the
upstream. The point this proves is that the realised request carries the injected
scope parameter regardless of what the agent asked for.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from acp_core.connector import ConnectorResult
from acp_core.enums import Kind
from acp_core.models import Actor, ResolvedAction
from acp_core.scope import ScopePredicate

Sender = Callable[[dict[str, Any]], list[dict[str, Any]]]


class HttpConnector:
    def __init__(self, base_url: str = "https://api.internal", sender: Sender | None = None) -> None:
        self.base_url = base_url
        self._sender = sender
        self.requests: list[dict[str, Any]] = []  # captured for assertions

    def execute(
        self, action: ResolvedAction, scope: ScopePredicate | None, actor: Actor
    ) -> ConnectorResult:
        params: dict[str, Any] = dict(action.data)
        if scope is not None:
            name, value = scope.query_param(actor)
            params[name] = value  # mandatory scope filter injected below the model
        request = {
            "method": "GET" if action.kind is Kind.OBSERVE else "POST",
            "url": f"{self.base_url}/{action.resource}",
            "params": params,
        }
        self.requests.append(request)
        if self._sender is not None:
            return ConnectorResult(kind="rows", rows=self._sender(request), query=str(request))
        return ConnectorResult(kind="receipt", receipt={"forwarded": True}, query=str(request))

    def dispatch(
        self, action: ResolvedAction, actor: Actor, idempotency_key: str
    ) -> ConnectorResult:
        request = {
            "method": "POST",
            "url": f"{self.base_url}/{action.resource}",
            "params": dict(action.data),
            "idempotency_key": idempotency_key,  # sent as a header server-side
        }
        self.requests.append(request)
        return ConnectorResult(kind="receipt", receipt={"forwarded": True}, query=str(request), handle=idempotency_key)

    def fetch_target(
        self, action: ResolvedAction, scope: ScopePredicate | None, actor: Actor
    ) -> Mapping[str, Any] | None:
        if scope is None:
            return dict(action.data)
        # Without a live server, the supplied target attributes are the source of
        # truth for the scope membership check.
        return dict(action.data) if scope.matches(action.data, actor) else None

    def cancel(self, handle: str) -> None:
        return None
