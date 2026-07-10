"""THE DEMO DRIVER — not a role, just the runnable check.

Starts the real gateway service (a uvicorn subprocess, real HTTP on
localhost), runs the unchanged old-tools agent against the interception
proxy, and verifies through the audit API that every call — including both
refusals — was translated, decided, and recorded.

Run:  python guide/06_keep_your_tools/main.py
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
    import agent  # the unchanged old-tools agent — no Stonefold imports inside

    port = free_port()
    gateway = start_gateway(port)
    try:
        base = f"http://127.0.0.1:{port}"
        results = agent.run(base)

        # the two allows went through the ordinary pipeline:
        assert results["lookup"]["decision"] == "allow"
        assert results["lookup"]["output"] == [
            {"id": "C1", "name": "Acme"}, {"id": "C2", "name": "Globex"},
        ]
        assert results["ticket"]["decision"] == "allow"

        # two refusals, two different walls: export_crm was MAPPED and the
        # policy refused it; run_sql was never mapped at all.
        assert results["export"]["decision"] == "deny"
        assert results["export"]["rule"] == "default-deny"
        assert results["sql"]["decision"] == "deny"
        assert results["sql"]["rule"] == "unmapped-tool"

        # every one of the four calls is on the audit record, over the wire:
        with urllib.request.urlopen(f"{base}/admin/trace/s1", timeout=5) as resp:
            trace = json.loads(resp.read().decode("utf-8"))
        assert len(trace) == 4 and all(r["actor"] == "rep-7" for r in trace)
        print(f"\ndriver: {len(trace)} audit records — the mapping translated,"
              " the same pipeline decided")
        print("ok: 06_keep_your_tools")
    finally:
        gateway.terminate()
        gateway.wait(timeout=10)


if __name__ == "__main__":
    main()
