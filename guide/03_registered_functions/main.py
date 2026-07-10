"""THE DEMO DRIVER — starts the real service, runs the agent over the wire,
verifies. Run:  python guide/03_registered_functions/main.py
"""

from __future__ import annotations

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
    import agent

    port = free_port()
    gateway = start_gateway(port)
    try:
        results = agent.run(f"http://127.0.0.1:{port}")

        # scope: only alice's row came back
        assert results["read"]["decision"] == "allow"
        assert results["read"]["output"] == [
            {"id": "N1", "owner_id": "alice", "text": "mine"}
        ]
        # content hook blocked the secret
        assert results["blocked"]["decision"] == "deny"
        assert results["blocked"]["rule"] == "gate:contentCheck"
        # the three check verdicts, over the wire
        assert results["O1"]["decision"] == "allow"
        assert results["O2"]["decision"] == "deny"
        assert results["O3"]["decision"] == "hold"
        assert results["O3"]["reasonCode"] == "stock-uncertain"
        assert results["O3"]["retryClass"] == "escalate"
        print("ok: 03_registered_functions")
    finally:
        gateway.terminate()
        gateway.wait(timeout=10)


if __name__ == "__main__":
    main()
