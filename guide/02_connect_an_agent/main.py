"""THE DEMO DRIVER — not a role, just the runnable check.

Starts the real gateway service (a uvicorn subprocess, real HTTP on
localhost), runs the agent against it over the wire, and verifies the
outcome. With DATABASE_URL set (see guide/docker-compose.yml) the same run
uses Postgres for the audit; without it the service says so and runs
in-memory.

Run:  python guide/02_connect_an_agent/main.py
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def start_gateway(port: int) -> subprocess.Popen[bytes]:
    """`uvicorn gateway_service:app` — exactly what the infra engineer runs."""
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


def main() -> None:
    sys.path.insert(0, str(HERE))
    import agent  # the agent developer's file — no Stonefold imports inside

    port = free_port()
    gateway = start_gateway(port)
    try:
        base = f"http://127.0.0.1:{port}"
        results = agent.run(base)

        # every step was decided by the gateway, over the wire:
        assert [r["decision"] for r in results] == ["allow", "allow", "allow"]
        assert results[0]["output"] == [
            {"id": "C1", "name": "Acme"}, {"id": "C2", "name": "Globex"},
        ]

        # the smuggled "actor": "admin" changed nothing — the audit trail
        # (read over the same HTTP surface) names the TRANSPORT identity:
        with urllib.request.urlopen(f"{base}/admin/trace/s1", timeout=5) as resp:
            trace = json.loads(resp.read().decode("utf-8"))
        assert trace and all(r["actor"] == "rep-7" for r in trace)
        print(f"\ndriver: {len(trace)} audit records, every one names actor rep-7")
        print("ok: 02_connect_an_agent")
    finally:
        gateway.terminate()
        gateway.wait(timeout=10)


if __name__ == "__main__":
    main()
