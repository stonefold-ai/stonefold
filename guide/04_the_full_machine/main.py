"""THE DEMO DRIVER — starts the real service, then plays BOTH seats over the
wire: the agent submits payments, the operator approves / rejects / kills.
Everything is verified through the audit API, because that is the only
truthful window an outside process has.

Run:  python guide/04_the_full_machine/main.py
      (with DATABASE_URL/REDIS_URL set, the same run uses Postgres + Redis)
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def start_gateway(port: int) -> subprocess.Popen[bytes]:
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "gateway_service:app",
         "--app-dir", str(HERE), "--port", str(port), "--log-level", "warning"],
    )
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/tool-schema", timeout=1):
                return proc
        except Exception:
            if proc.poll() is not None:
                raise RuntimeError("gateway service exited during startup")
            time.sleep(0.25)
    proc.terminate()
    raise RuntimeError("gateway service did not become ready")


def wait_for(what: str, predicate: Callable[[], bool], timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for: {what}")


def main() -> None:
    sys.path.insert(0, str(HERE))
    import agent
    import operator_console as op

    port = free_port()
    gateway = start_gateway(port)
    base = f"http://127.0.0.1:{port}"
    try:
        def successes(session: str) -> int:
            return sum(1 for r in op.trace(base, session) if r["outcome"] == "success")

        tool = agent.GatewayTool(base, actor="ap-bot", session="s1")

        # 1. small payment: accepted + staged; the WORKER moves the money.
        small = agent.pay(tool, 400)
        assert small["decision"] == "allow" and small["ticket"]
        wait_for("the worker to dispatch the $400", lambda: successes("s1") == 1)
        print("driver: worker dispatched the $400 (audit outcome=success)")

        # 2. big payment: HELD. The operator sees it, and releases it.
        big = agent.pay(tool, 5000)
        assert big["decision"] == "hold"
        held = op.list_approvals(base)
        assert [row["id"] for row in held] == [big["ticket"]]
        op.approve(base, big["ticket"], "manager-1")
        wait_for("the released $5000 to dispatch", lambda: successes("s1") == 2)
        print("driver: operator approved; worker dispatched the $5000")

        # 3. rejection means it NEVER moves.
        rejected = agent.pay(tool, 9000)
        op.reject(base, rejected["ticket"], "manager-1")
        time.sleep(0.6)  # give the worker every chance to be wrong
        assert successes("s1") == 2
        print("driver: operator rejected; nothing dispatched")

        # 4. the kill switch, over the wire.
        order_id = op.kill_session(base, "s1")
        halted = agent.pay(tool, 50)
        assert halted["decision"] == "halt"
        op.lift(base, order_id)
        tool2 = agent.GatewayTool(base, actor="ap-bot", session="s2")
        assert agent.pay(tool2, 50)["decision"] == "allow"
        print("driver: kill halted s1; lift restored; s2 unaffected")

        print("ok: 04_the_full_machine")
    finally:
        gateway.terminate()
        gateway.wait(timeout=10)


if __name__ == "__main__":
    main()
