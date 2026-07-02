"""A FastAPI harness exposing any ``ConformanceDriver`` over the TCK wire
protocol (``acp_tck.http_driver``).

Two uses: (1) it serves the reference implementation to remote TCK runs, and
(2) it is the golden example of the harness API a non-Python gateway must
expose. TEST BUILDS ONLY — this surface can seed rows and reset state by
design; it must never exist in a production deployment.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import FastAPI

from acp_tck.driver import ConformanceDriver, Operation, TckActor


def create_tck_harness(driver: ConformanceDriver, *, implementation: str) -> FastAPI:
    app = FastAPI(title="ACP TCK harness (test builds only)", docs_url=None, redoc_url=None)

    @app.get("/tck/capabilities")
    def capabilities() -> dict[str, Any]:
        return {"implementation": implementation, "capabilities": sorted(driver.capabilities())}

    @app.post("/tck/load")
    def load(body: dict[str, Any]) -> dict[str, Any]:
        result = driver.load(str(body["registryYaml"]), str(body["policyYaml"]))
        return {"ok": result.ok, "errors": list(result.errors), "warnings": list(result.warnings)}

    @app.post("/tck/clock")
    def clock(body: dict[str, Any]) -> dict[str, Any]:
        driver.set_clock(datetime.fromisoformat(str(body["now"])))
        return {}

    @app.post("/tck/seed")
    def seed(body: dict[str, Any]) -> dict[str, Any]:
        driver.seed(str(body["resource"]), list(body["rows"]))
        return {}

    @app.post("/tck/submit")
    def submit(body: dict[str, Any]) -> dict[str, Any]:
        actor_raw = dict(body["actor"])
        op_raw = dict(body["op"])
        result = driver.submit(
            TckActor(
                id=str(actor_raw["id"]),
                roles=frozenset(str(r) for r in actor_raw.get("roles", [])),
                claims=dict(actor_raw.get("claims", {})),
            ),
            str(body["sessionId"]),
            Operation(
                resource=str(op_raw["resource"]),
                action=op_raw.get("action"),
                data=dict(op_raw.get("data", {})),
                target=op_raw.get("target"),
                sink=op_raw.get("sink"),
                context=dict(op_raw.get("context", {})),
            ),
        )
        return {
            "decision": result.decision,
            "ticket": result.ticket,
            "rows": None if result.rows is None else [dict(r) for r in result.rows],
            "reason": result.reason,
        }

    @app.post("/tck/approve")
    def approve(body: dict[str, Any]) -> dict[str, Any]:
        return {"accepted": driver.approve(str(body["ticket"]), str(body["approverId"]))}

    @app.post("/tck/reject")
    def reject(body: dict[str, Any]) -> dict[str, Any]:
        return {"accepted": driver.reject(str(body["ticket"]), str(body["approverId"]))}

    @app.post("/tck/dispatch")
    def dispatch(body: dict[str, Any]) -> dict[str, Any]:
        return {"settled": driver.dispatch_once()}

    @app.get("/tck/effects")
    def effects() -> dict[str, Any]:
        return {"effects": [dict(e) for e in driver.effects()]}

    @app.post("/tck/kill")
    def kill(body: dict[str, Any]) -> dict[str, Any]:
        kill_id = driver.kill(
            scope=str(body["scope"]),
            agent=body.get("agent"),
            session_id=body.get("sessionId"),
            resource=body.get("resource"),
            action=body.get("action"),
            issued_by=str(body.get("issuedBy", "tck-operator")),
        )
        return {"killId": kill_id}

    @app.post("/tck/lift")
    def lift(body: dict[str, Any]) -> dict[str, Any]:
        driver.lift(str(body["killId"]))
        return {}

    @app.get("/tck/audit")
    def audit() -> dict[str, Any]:
        return {
            "records": [
                {
                    "decision": r.decision,
                    "resource": r.resource,
                    "action": r.action,
                    "outcome": r.outcome,
                    "reason": r.reason,
                }
                for r in driver.audit()
            ]
        }

    @app.post("/tck/inject-dispatch-failure")
    def inject(body: dict[str, Any]) -> dict[str, Any]:
        driver.inject_dispatch_failure(str(body["action"]))
        return {}

    @app.post("/tck/update-set")
    def update_set(body: dict[str, Any]) -> dict[str, Any]:
        driver.update_named_set(str(body["name"]), [str(v) for v in body["values"]])
        return {}

    return app
