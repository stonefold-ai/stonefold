# SPDX-License-Identifier: Apache-2.0
"""In-memory connector (design §5). Test double + the default for resources with
no external system. Applies the injected scope as a row filter / membership test;
holds no policy logic (CLAUDE.md)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from stonefold_core.connector import ConnectorResult, ScopeCapability, ScopeLostError
from stonefold_core.enums import Kind
from stonefold_core.models import Actor, ResolvedAction
from stonefold_core.scope import ScopePredicate

_TARGET_KEYS = ("id", "targetId", "target")


def _target_id(action: ResolvedAction) -> Any:
    for key in _TARGET_KEYS:
        if key in action.data:
            return action.data[key]
    # also accept ``<resource>Id`` (e.g. accountId)
    camel = f"{action.resource[:1].lower()}{action.resource[1:]}Id"
    return action.data.get(camel)


class InMemoryConnector:
    """Tables keyed by resource; each row is a dict carrying its scope column."""

    def __init__(
        self,
        tables: Mapping[str, list[dict[str, Any]]] | None = None,
        *,
        scope_capability: ScopeCapability | None = None,
    ) -> None:
        self.tables: dict[str, list[dict[str, Any]]] = {
            k: [dict(r) for r in v] for k, v in (tables or {}).items()
        }
        # effects recorded for visibility; real dispatch goes via ``dispatch``.
        self.effects: list[dict[str, Any]] = []
        self._dispatched: dict[str, ConnectorResult] = {}
        # CS-018: this connector stands in for either connector class in tests —
        # SQL-class (transactional, the default: single-process membership test
        # is atomic here) or, when overridden, a declared-window connector.
        self.scope_capability = scope_capability or ScopeCapability.transactional()

    def execute(
        self, action: ResolvedAction, scope: ScopePredicate | None, actor: Actor
    ) -> ConnectorResult:
        if action.kind is Kind.OBSERVE:
            rows = self.tables.get(action.resource, [])
            if scope is not None:
                rows = [r for r in rows if scope.matches(r, actor)]
            return ConnectorResult(kind="rows", rows=[dict(r) for r in rows])
        if action.kind is Kind.RECORD:
            row = dict(action.data)
            self.tables.setdefault(action.resource, []).append(row)
            return ConnectorResult(
                kind="receipt", receipt={"created": True, "resource": action.resource}
            )
        if action.kind is Kind.TRANSITION:
            target = self._find(action, scope, actor)
            if target is None:
                return ConnectorResult(kind="receipt", receipt={"transitioned": False})
            target["state"] = action.data.get("to") or action.action
            return ConnectorResult(
                kind="receipt", receipt={"transitioned": True, "state": target["state"]}
            )
        # EFFECT — recorded only; staging/dispatch is M4.
        self.effects.append(
            {"resource": action.resource, "action": action.action, "data": dict(action.data)}
        )
        return ConnectorResult(kind="receipt", receipt={"sent": True})

    def dispatch(
        self, action: ResolvedAction, actor: Actor, idempotency_key: str
    ) -> ConnectorResult:
        return self._dispatch(action, actor, idempotency_key, None)

    def dispatch_scoped(
        self, action: ResolvedAction, actor: Actor, idempotency_key: str,
        scope: ScopePredicate,
    ) -> ConnectorResult:
        # CS-018 transactional form: the membership test and the effect are one
        # atomic step here (single process) — the in-memory analogue of ANDing
        # the predicate into the effect's UPDATE.
        return self._dispatch(action, actor, idempotency_key, scope)

    def _dispatch(
        self, action: ResolvedAction, actor: Actor, idempotency_key: str,
        scope: ScopePredicate | None,
    ) -> ConnectorResult:
        # Idempotent on the key: a worker retry returns the prior result and does
        # NOT append a second effect (design §9, acceptance D1).
        if idempotency_key in self._dispatched:
            return self._dispatched[idempotency_key]
        # Re-assert only when the effect names a target: a targetless effect
        # (a pure send, an auto-staged compensation) has no row the predicate
        # could select, so there is nothing to re-assert.
        if scope is not None and _target_id(action) is not None:
            if self._find(action, scope, actor) is None:
                raise ScopeLostError(
                    f"{action.resource} target is no longer in the actor's scoped set"
                )
        self.effects.append(
            {"resource": action.resource, "action": action.action, "data": dict(action.data), "key": idempotency_key}
        )
        result = ConnectorResult(
            kind="receipt", receipt={"sent": True}, handle=idempotency_key
        )
        self._dispatched[idempotency_key] = result
        return result

    def fetch_target(
        self, action: ResolvedAction, scope: ScopePredicate | None, actor: Actor
    ) -> Mapping[str, Any] | None:
        return self._find(action, scope, actor)

    def _find(
        self, action: ResolvedAction, scope: ScopePredicate | None, actor: Actor
    ) -> dict[str, Any] | None:
        target_id = _target_id(action)
        if target_id is None:
            return None
        for row in self.tables.get(action.resource, []):
            if str(row.get("id")) == str(target_id):
                if scope is None or scope.matches(row, actor):
                    return row
                return None  # exists but outside the actor's scope
        return None

    def cancel(self, handle: str) -> None:
        return None
