"""The language-neutral driver binding: the TCK over HTTP/JSON.

A gateway written in ANY language certifies by exposing the small, TEST-ONLY
harness API below (docs/12 §6) and running::

    from stonefold_tck import run_conformance
    from stonefold_tck.http_driver import HttpDriver
    print(run_conformance(HttpDriver("http://localhost:9099")).render())

The harness API is a build-time test surface — it must never ship in a
production build (it can seed rows and reset state by design).

Wire protocol (all JSON; camelCase keys):

| Method | Path                            | Request → Response |
|--------|---------------------------------|--------------------|
| GET    | /tck/capabilities               | → {implementation, capabilities: [str]} |
| POST   | /tck/load                       | {registryYaml, policyYaml} → {ok, errors, warnings} |
| POST   | /tck/clock                      | {now: ISO-8601} → {} |
| POST   | /tck/seed                       | {resource, rows: [obj]} → {} (replaces the resource's prior rows) |
| POST   | /tck/submit                     | {actor: {id, roles, claims}, sessionId, op: {resource, action?, data, target?, sink?, context}} → {decision, ticket?, rows?, reason, reasonCode?, retryClass?, agentView?} |
| POST   | /tck/approve                    | {ticket, approverId} → {accepted: bool} |
| POST   | /tck/reject                     | {ticket, approverId} → {accepted: bool} |
| POST   | /tck/resolve                    | {ticket, resolverId, gate} → {accepted: bool} |
| POST   | /tck/sweep-holds                | {} → {handled: int} |
| POST   | /tck/seed-obligations           | {registry, records: {ref: fields}} → {} |
| POST   | /tck/obligation-outage          | {registry, active: bool} → {} |
| POST   | /tck/dispatch                   | {} → {settled: int} |
| GET    | /tck/effects                    | → {effects: [{resource, action, data}]} |
| POST   | /tck/kill                       | {scope, agent?, sessionId?, resource?, action?, issuedBy} → {killId} |
| POST   | /tck/lift                       | {killId} → {} |
| GET    | /tck/audit                      | → {records: [{decision, resource, action, outcome, reason?}]} |
| POST   | /tck/inject-dispatch-failure    | {action} → {} |
| POST   | /tck/update-set                 | {name, values: [str]} → {} |
| POST   | /tck/submit-batch               | {actor, sessionId, ops: [op]} → {decision, failingIndex?, results: [per-op /tck/submit responses]} |
| GET    | /tck/connector-digest/{name}    | → {digest: "sha256:<hex>"} |
| POST   | /tck/tamper-connector           | {name} → {} |

Omit an endpoint (404/501) only if its capability is not advertised.
(``reason`` carries the deciding rule/settle reason; required for the
``freshness``/``scope-reassert`` capabilities. ``/tck/update-set`` backs
``freshness``; ``/tck/submit-batch`` backs ``batch`` (v0.5 CS-023);
``/tck/connector-digest`` + ``/tck/tamper-connector`` back ``digest-pinning``
(v0.5 CS-020); ``/tck/resolve`` + ``/tck/sweep-holds`` back
``hold-precondition``, ``reasonCode``/``retryClass``/``agentView`` back
``feedback``, and ``/tck/seed-obligations`` + ``/tck/obligation-outage`` back
``obligation`` (v0.6).)
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

from stonefold_tck.driver import (
    AuditEntry,
    BatchSubmitResult,
    LoadResult,
    Operation,
    SubmitResult,
    TckActor,
)

# transport: (method, path, payload-or-None) -> parsed JSON body
Transport = Callable[[str, str, Mapping[str, Any] | None], Mapping[str, Any]]


def _submit_result(body: Mapping[str, Any]) -> SubmitResult:
    rows = body.get("rows")
    retry = body.get("retryClass")
    return SubmitResult(
        decision=str(body.get("decision", "")),
        ticket=body.get("ticket"),
        rows=None if rows is None else [dict(r) for r in rows],
        reason=str(body.get("reason", "")),
        reason_code=str(body.get("reasonCode", "") or ""),
        retry_class=None if retry is None else str(retry),
        agent_view=str(body.get("agentView", "") or ""),
    )


def _urllib_transport(base_url: str, timeout_s: float) -> Transport:
    base = base_url.rstrip("/")

    def call(method: str, path: str, payload: Mapping[str, Any] | None) -> Mapping[str, Any]:
        body = None if payload is None else json.dumps(dict(payload)).encode("utf-8")
        request = urllib.request.Request(
            base + path,
            data=body,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            raw = response.read()
        result: Mapping[str, Any] = json.loads(raw.decode("utf-8")) if raw else {}
        return result

    return call


class HttpDriver:
    """``ConformanceDriver`` speaking the harness wire protocol.

    ``transport`` is injectable so an in-process test client (e.g. FastAPI's)
    can stand in for a real socket.
    """

    def __init__(
        self,
        base_url: str = "",
        *,
        transport: Transport | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        if transport is None and not base_url:
            raise ValueError("HttpDriver needs a base_url or an explicit transport")
        self._call: Transport = transport or _urllib_transport(base_url, timeout_s)

    # --- driver contract ---------------------------------------------------
    def capabilities(self) -> frozenset[str]:
        body = self._call("GET", "/tck/capabilities", None)
        return frozenset(str(c) for c in body.get("capabilities", []))

    def implementation_name(self) -> str:
        body = self._call("GET", "/tck/capabilities", None)
        return str(body.get("implementation", "implementation-under-test"))

    def load(self, registry_yaml: str, policy_yaml: str) -> LoadResult:
        body = self._call(
            "POST", "/tck/load", {"registryYaml": registry_yaml, "policyYaml": policy_yaml}
        )
        return LoadResult(
            ok=bool(body.get("ok")),
            errors=[str(e) for e in body.get("errors", [])],
            warnings=[str(w) for w in body.get("warnings", [])],
        )

    def set_clock(self, now: datetime) -> None:
        self._call("POST", "/tck/clock", {"now": now.isoformat()})

    def seed(self, resource: str, rows: Sequence[Mapping[str, Any]]) -> None:
        self._call("POST", "/tck/seed", {"resource": resource, "rows": [dict(r) for r in rows]})

    def submit(self, actor: TckActor, session_id: str, op: Operation) -> SubmitResult:
        body = self._call(
            "POST",
            "/tck/submit",
            {
                "actor": {
                    "id": actor.id,
                    "roles": sorted(actor.roles),
                    "claims": dict(actor.claims),
                },
                "sessionId": session_id,
                "op": {
                    "resource": op.resource,
                    "action": op.action,
                    "data": dict(op.data),
                    "target": op.target,
                    "sink": op.sink,
                    "context": dict(op.context),
                },
            },
        )
        return _submit_result(body)

    def approve(self, ticket: str, approver_id: str) -> bool:
        body = self._call("POST", "/tck/approve", {"ticket": ticket, "approverId": approver_id})
        return bool(body.get("accepted"))

    def reject(self, ticket: str, approver_id: str) -> bool:
        body = self._call("POST", "/tck/reject", {"ticket": ticket, "approverId": approver_id})
        return bool(body.get("accepted"))

    def dispatch_once(self) -> int:
        body = self._call("POST", "/tck/dispatch", {})
        return int(body.get("settled", 0))

    def effects(self) -> Sequence[Mapping[str, Any]]:
        body = self._call("GET", "/tck/effects", None)
        return [dict(e) for e in body.get("effects", [])]

    def kill(
        self,
        *,
        scope: str,
        agent: str | None = None,
        session_id: str | None = None,
        resource: str | None = None,
        action: str | None = None,
        issued_by: str = "tck-operator",
    ) -> str:
        body = self._call(
            "POST",
            "/tck/kill",
            {
                "scope": scope,
                "agent": agent,
                "sessionId": session_id,
                "resource": resource,
                "action": action,
                "issuedBy": issued_by,
            },
        )
        return str(body["killId"])

    def lift(self, kill_id: str) -> None:
        self._call("POST", "/tck/lift", {"killId": kill_id})

    def audit(self) -> Sequence[AuditEntry]:
        body = self._call("GET", "/tck/audit", None)
        return [
            AuditEntry(
                decision=str(r.get("decision", "")),
                resource=r.get("resource"),
                action=r.get("action"),
                outcome=str(r.get("outcome", "")),
                reason=str(r.get("reason") or ""),
            )
            for r in body.get("records", [])
        ]

    def submit_batch(
        self, actor: TckActor, session_id: str, ops: Sequence[Operation]
    ) -> BatchSubmitResult:
        body = self._call(
            "POST",
            "/tck/submit-batch",
            {
                "actor": {
                    "id": actor.id,
                    "roles": sorted(actor.roles),
                    "claims": dict(actor.claims),
                },
                "sessionId": session_id,
                "ops": [
                    {
                        "resource": op.resource,
                        "action": op.action,
                        "data": dict(op.data),
                        "target": op.target,
                        "sink": op.sink,
                        "context": dict(op.context),
                    }
                    for op in ops
                ],
            },
        )
        failing = body.get("failingIndex")
        return BatchSubmitResult(
            decision=str(body.get("decision", "")),
            failing_index=None if failing is None else int(failing),
            results=[_submit_result(r) for r in body.get("results", [])],
        )

    def connector_digest(self, name: str) -> str:
        body = self._call("GET", f"/tck/connector-digest/{name}", None)
        return str(body["digest"])

    def tamper_connector(self, name: str) -> None:
        self._call("POST", "/tck/tamper-connector", {"name": name})

    def inject_dispatch_failure(self, action: str) -> None:
        self._call("POST", "/tck/inject-dispatch-failure", {"action": action})

    def update_named_set(self, name: str, values: Sequence[str]) -> None:
        self._call("POST", "/tck/update-set", {"name": name, "values": list(values)})

    def resolve(self, ticket: str, resolver_id: str, gate: str) -> bool:
        body = self._call(
            "POST", "/tck/resolve",
            {"ticket": ticket, "resolverId": resolver_id, "gate": gate},
        )
        return bool(body.get("accepted"))

    def sweep_holds(self) -> int:
        body = self._call("POST", "/tck/sweep-holds", {})
        return int(body.get("handled", 0))

    def seed_obligations(
        self, registry: str, records: Mapping[str, Mapping[str, Any]]
    ) -> None:
        self._call(
            "POST", "/tck/seed-obligations",
            {"registry": registry,
             "records": {ref: dict(fields) for ref, fields in records.items()}},
        )

    def set_obligation_outage(self, registry: str, active: bool) -> None:
        self._call(
            "POST", "/tck/obligation-outage", {"registry": registry, "active": active}
        )
