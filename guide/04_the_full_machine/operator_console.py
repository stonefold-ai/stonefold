"""THE OPERATOR — the human control plane, over the same HTTP service.

These are the endpoints an approvals inbox UI or an on-call runbook calls.
No Stonefold imports here either: the operator surface is just HTTP, so it
plugs into whatever console/chat-ops tooling you already have.

    python operator_console.py http://localhost:8099 list
    python operator_console.py http://localhost:8099 approve <ticket> manager-1
    python operator_console.py http://localhost:8099 kill s1
"""

from __future__ import annotations

import json
import sys
import urllib.request
from typing import Any


def _call(base: str, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    request = urllib.request.Request(
        base.rstrip("/") + path,
        data=None if payload is None else json.dumps(payload).encode("utf-8"),
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def list_approvals(base: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = _call(base, "GET", "/admin/approvals")
    return rows


def approve(base: str, ticket: str, approver: str) -> dict[str, Any]:
    result: dict[str, Any] = _call(
        base, "POST", f"/admin/approvals/{ticket}/approve", {"approver": approver}
    )
    return result


def reject(base: str, ticket: str, approver: str) -> dict[str, Any]:
    result: dict[str, Any] = _call(
        base, "POST", f"/admin/approvals/{ticket}/reject", {"approver": approver}
    )
    return result


def kill_session(base: str, session_id: str, issued_by: str = "operator") -> str:
    order: dict[str, Any] = _call(
        base, "POST", "/kill",
        {"scope": "session", "session_id": session_id, "issued_by": issued_by},
    )
    return str(order["id"])


def lift(base: str, order_id: str, lifted_by: str = "operator") -> None:
    _call(base, "POST", f"/kill/{order_id}/lift", {"lifted_by": lifted_by})


def trace(base: str, correlation_id: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = _call(base, "GET", f"/admin/trace/{correlation_id}")
    return records


if __name__ == "__main__":
    base_url, command = sys.argv[1], sys.argv[2]
    if command == "list":
        for row in list_approvals(base_url):
            print(row["id"], row["agent"], row["resolved"]["data"])
    elif command == "approve":
        approve(base_url, sys.argv[3], sys.argv[4])
    elif command == "reject":
        reject(base_url, sys.argv[3], sys.argv[4])
    elif command == "kill":
        print(kill_session(base_url, sys.argv[3]))
    elif command == "lift":
        lift(base_url, sys.argv[3])
