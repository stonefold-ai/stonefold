"""The TCK certifies the reference implementation (docs/12).

This is both the reference's certification and the kit's own self-test: every
profile must come back CERTIFIED (all checks pass, none skipped — the
reference driver advertises every capability).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from stonefold_tck import ALL_PROFILES, run_conformance
from stonefold_tck.adapters.http_harness import create_tck_harness
from stonefold_tck.adapters.reference import ReferenceDriver
from stonefold_tck.fixtures import TCK_REGISTRY
from stonefold_tck.http_driver import HttpDriver


def test_reference_certifies_every_profile() -> None:
    report = run_conformance(ReferenceDriver(), implementation="stonefold-reference (python)")
    assert not report.failures, "\n" + report.render()
    assert set(report.certified_profiles()) == set(ALL_PROFILES), "\n" + report.render()


def test_wire_binding_certifies_end_to_end() -> None:
    """The language-neutral path: the whole suite through the HTTP wire
    protocol (HttpDriver → harness API → driver) — what a Java/Go/Rust
    gateway exercises, minus the socket."""
    from fastapi.testclient import TestClient

    app = create_tck_harness(ReferenceDriver(), implementation="stonefold-reference (http)")
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


def test_the_dialect_bridge_carries_every_registry_field() -> None:
    """A guard against a gap that has now bitten four times.

    ``authoring_to_compact`` translates the spec's authoring registry into the
    loader's compact dialect, and each time a field was added to the registry
    model — per-action ``data``, ``label``, ``closure``, ``items``, and top-level
    ``sources`` — the bridge silently dropped it. Silently is the problem: the
    registry loads, the declaration is simply absent, and whatever reads it
    behaves as though nobody declared anything.

    So this test fails when a field is added to ``RegistryFile`` and not carried,
    which is cheaper than finding out from a gate that never fired.
    """
    from stonefold_core.registry import RegistryFile
    from stonefold_tck.adapters.reference import authoring_to_compact
    import yaml as _yaml

    bridged = authoring_to_compact(_yaml.safe_load(TCK_REGISTRY))

    # Fields the authoring dialect deliberately cannot express, with the reason.
    not_in_authoring = {
        # split out of `preconditionChecks` by the loader, not authored separately
        "precondition_decls",
        # authored as `valueSets.resultSensitivity`, bridged as `classifications`
        "classifications",
        # authored inside `connectors:` as a per-connector `digest`
        "connector_digests",
    }
    expected = set(RegistryFile.model_fields) - not_in_authoring
    missing = sorted(expected - set(bridged))
    assert not missing, (
        f"authoring_to_compact drops {missing} — a registry in the spec's own "
        f"authoring format would silently lose {missing} on the way to the loader"
    )
