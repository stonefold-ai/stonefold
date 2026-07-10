"""THE DEMO DRIVER — starts the real service, runs the agent's convergence
loop over the wire, and reads the consumption receipt back out of the audit
API. Run:  python guide/05_obligation_matching/main.py
"""

from __future__ import annotations

import json
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
    # the service is a separate OS process on a real TCP port — the agent
    # only ever reaches it over HTTP, never in-process.
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

    port = free_port()
    gateway = start_gateway(port)
    base = f"http://127.0.0.1:{port}"
    try:
        def trace(session: str) -> list[dict[str, Any]]:
            with urllib.request.urlopen(f"{base}/admin/trace/{session}", timeout=5) as r:
                records: list[dict[str, Any]] = json.loads(r.read().decode("utf-8"))
            return records

        tool = agent.GatewayTool(base, actor="ap-bot", session="s1")

        # BEAT 1+2 — the convergence loop: wrong amount refused RETRYABLE,
        # corrected amount matches the open PO line and is accepted.
        final = agent.converge(tool, extracted_amount=990.0)
        assert final["decision"] == "allow" and final["ticket"]

        # the worker dispatches it and CONSUMES the line with the settle —
        # the receipt is in the audit record, over the same HTTP surface:
        def consumed() -> bool:
            return any(
                r["outcome"] == "success"
                and r.get("consumption", {}) and r["consumption"]["state"] == "consumed"
                for r in trace("s1")
            )
        wait_for("dispatch + consumption", consumed)
        receipt = next(r["consumption"] for r in trace("s1")
                       if r["outcome"] == "success")
        print(f"driver: line consumed, receipt {receipt['receipt'][:16]}...")

        # BEAT 3 — the SAME invoice again: the line is spent, nothing
        # matches, and the class says TERMINAL (stop resubmitting). This is
        # the refusal no pre-v0.6 gate could produce.
        again = agent.pay(tool, 800.0)
        assert again["decision"] == "deny"
        assert again["reasonCode"] == "no-match"
        assert again["retryClass"] == "terminal"

        # BEAT 4 — a fraudulent invoice (no order exists at all): under every
        # limit, matching nothing. Money was never at risk.
        fraud = agent.pay(tool, 4500.0, "QUICKPAY")
        assert fraud["decision"] == "deny" and fraud["reasonCode"] == "no-match"
        successes = [r for r in trace("s1") if r["outcome"] == "success"]
        assert len(successes) == 1  # exactly one payment ever left
        print("driver: one line, one payment, ever")
        print("ok: 05_obligation_matching")
    finally:
        gateway.terminate()
        gateway.wait(timeout=10)


if __name__ == "__main__":
    main()
