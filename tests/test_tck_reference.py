"""The TCK certifies the reference implementation (docs/12).

This is both the reference's certification and the kit's own self-test: every
profile must come back CERTIFIED (all checks pass, none skipped — the
reference driver advertises every capability).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from acp_tck import ALL_PROFILES, run_conformance
from acp_tck.adapters.http_harness import create_tck_harness
from acp_tck.adapters.reference import ReferenceDriver
from acp_tck.http_driver import HttpDriver


def test_reference_certifies_every_profile() -> None:
    report = run_conformance(ReferenceDriver(), implementation="acp-reference (python)")
    assert not report.failures, "\n" + report.render()
    assert set(report.certified_profiles()) == set(ALL_PROFILES), "\n" + report.render()


def test_wire_binding_certifies_end_to_end() -> None:
    """The language-neutral path: the whole suite through the HTTP wire
    protocol (HttpDriver → harness API → driver) — what a Java/Go/Rust
    gateway exercises, minus the socket."""
    from fastapi.testclient import TestClient

    app = create_tck_harness(ReferenceDriver(), implementation="acp-reference (http)")
    client = TestClient(app)

    def transport(method: str, path: str, payload: Mapping[str, Any] | None) -> Mapping[str, Any]:
        if method == "GET":
            response = client.get(path)
        else:
            response = client.post(path, json=dict(payload or {}))
        assert response.status_code == 200, f"{method} {path} -> {response.status_code}: {response.text}"
        body: Mapping[str, Any] = response.json() if response.content else {}
        return body

    driver = HttpDriver(transport=transport)
    report = run_conformance(driver, implementation=driver.implementation_name())
    assert not report.failures, "\n" + report.render()
    assert set(report.certified_profiles()) == set(ALL_PROFILES), "\n" + report.render()
