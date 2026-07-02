"""CS-020 — connector digest pinning verification.

Spec: ``docs/RFC-changeset-v0.4-to-v0.5.md`` §CS-020 + ``docs/06`` §5.

A connector declaration MAY pin its implementing artifact by content digest
(``digest: "sha256:<64 hex>"``). When declared, the gateway MUST verify the loaded
connector against the digest **at policy load and at dispatch**; a mismatch is a
**dependency failure** under ``failureMode`` (RFC §10) — fail closed by default,
audited. When no digest is declared, nothing changes (the overwhelming majority of
registries): these tests pin that "no digest ⇒ no check" regression too.

The reference implementation hashes the connector module's source bytes
(``acp_core.digest``); the RFC deliberately leaves artifact identity to the
implementation, so tests compute the *expected* digest from the artifact under test
rather than hard-coding a hex value.
"""

from __future__ import annotations

from typing import Any

import pytest

from acp_core import (
    Actor,
    Compensation,
    Connectors,
    Decision,
    InMemoryAuditSink,
    RawCall,
    Session,
    enforce,
    load_policy,
    load_registry,
)
from acp_core.digest import (
    DIGEST_MISMATCH,
    DigestMismatch,
    DigestMismatchError,
    artifact_digest,
    assert_connector_digests,
    digest_matches,
    pinned_connector_mismatch,
    verify_connector_digests,
)
from acp_core.outbox import PendingState
from acp_core.policy import FailureMode
from acp_connectors import InMemoryConnector
from acp_gates.engine import DefaultGateEngine
from acp_store import DispatchWorker, InMemoryOutboxStore
from tests.conftest import REGISTRY_DIR, full_registry, load_schema, load_yaml

BOGUS = "sha256:" + "0" * 64


def _connector_of(resource: str, action: str) -> str:
    """The connector name the compact registry pins on an action."""
    return full_registry().resolve(RawCall(resource=resource, action=action)).connector


def _registry_with_digests(digests: dict[str, str]) -> Any:
    """The shipped compact registry with connector digests injected."""
    data = load_yaml(REGISTRY_DIR / "acp-registry.yaml")
    data["connector_digests"] = digests
    return load_registry(data)


# --- A. artifact-digest primitives (pure) ---------------------------------
def test_artifact_digest_is_stable_and_class_specific() -> None:
    d = artifact_digest(InMemoryConnector())
    assert d.startswith("sha256:")
    assert len(d) == len("sha256:") + 64
    # same class ⇒ same digest (per-instance state does not enter the hash)
    assert artifact_digest(InMemoryConnector()) == d

    class _Fake:  # a different implementing module region ⇒ different digest
        pass

    assert artifact_digest(_Fake()) != d


def test_digest_matches_is_case_insensitive_and_exact() -> None:
    conn = InMemoryConnector()
    good = artifact_digest(conn)
    assert digest_matches(conn, good) is True
    assert digest_matches(conn, good.upper()) is True  # hex case ignored
    assert digest_matches(conn, BOGUS) is False


def test_digest_matches_uncomputable_artifact_is_false() -> None:
    # A control you cannot evaluate must not be assumed present: when the artifact
    # digest cannot be computed (e.g. a built-in with no source), treat it as a
    # mismatch (→ fail closed), never as a pass.
    assert digest_matches(object(), BOGUS) is False


# --- B. registry parsing --------------------------------------------------
_MINI = {
    "connectors": {"eff": {"type": "method", "digest": BOGUS}, "plain": {"type": "sql"}},
    "resources": {
        "Widget": {"connector": "eff", "actions": {"ship": {"kind": "effect"}}},
        "Gadget": {"connector": "plain", "actions": {"ship": {"kind": "effect"}}},
    },
}


def test_map_form_connectors_parse_names_and_digests() -> None:
    reg = load_registry(_MINI)
    assert set(reg.file.connectors) == {"eff", "plain"}
    assert reg.file.connector_digests == {"eff": BOGUS}
    assert reg.connector_digest("eff") == BOGUS
    assert reg.connector_digest("plain") is None


def test_explicit_connector_digests_key_is_accepted() -> None:
    reg = load_registry(
        {"connectors": ["eff"], "connector_digests": {"eff": BOGUS},
         "resources": {"Widget": {"connector": "eff", "actions": {"ship": {"kind": "effect"}}}}}
    )
    assert reg.connector_digest("eff") == BOGUS


def test_list_form_connectors_have_no_digests() -> None:
    reg = full_registry()  # the shipped list-form registry
    assert reg.file.connector_digests == {}
    assert reg.connector_digest("sql") is None


def test_resolve_carries_the_pinned_digest() -> None:
    reg = load_registry(_MINI)
    assert reg.resolve(RawCall(resource="Widget", action="ship")).connector_digest == BOGUS
    assert reg.resolve(RawCall(resource="Gadget", action="ship")).connector_digest is None


# --- C. verify / assert (load-time) ---------------------------------------
def test_verify_returns_empty_on_match() -> None:
    conn = InMemoryConnector()
    reg = _registry_with_digests({"sql": artifact_digest(conn)})
    assert verify_connector_digests(reg, Connectors({"sql": conn})) == []


def test_verify_returns_a_mismatch() -> None:
    conn = InMemoryConnector()
    reg = _registry_with_digests({"sql": BOGUS})
    mismatches = verify_connector_digests(reg, Connectors({"sql": conn}))
    assert len(mismatches) == 1
    m = mismatches[0]
    assert isinstance(m, DigestMismatch)
    assert m.connector == "sql" and m.expected == BOGUS
    assert m.actual == artifact_digest(conn)


def test_verify_skips_a_pinned_but_unwired_connector() -> None:
    # A digest declared for a connector the deployment did not wire cannot be
    # checked here; the unknown-connector path fails closed at dispatch instead.
    reg = _registry_with_digests({"ghost": BOGUS})
    assert verify_connector_digests(reg, Connectors({"sql": InMemoryConnector()})) == []


def test_assert_raises_and_audits_when_closed() -> None:
    audit = InMemoryAuditSink()
    reg = _registry_with_digests({"sql": BOGUS})
    with pytest.raises(DigestMismatchError) as exc:
        assert_connector_digests(
            reg, Connectors({"sql": InMemoryConnector()}),
            failure_mode=FailureMode.CLOSED, audit=audit,
        )
    assert exc.value.mismatches[0].connector == "sql"
    denials = [r for r in audit.records if r.decision is Decision.DENY]
    assert len(denials) == 1 and denials[0].rule == DIGEST_MISMATCH


def test_assert_returns_mismatches_without_raising_when_open() -> None:
    audit = InMemoryAuditSink()
    reg = _registry_with_digests({"sql": BOGUS})
    mismatches = assert_connector_digests(
        reg, Connectors({"sql": InMemoryConnector()}),
        failure_mode=FailureMode.OPEN, audit=audit,
    )
    assert len(mismatches) == 1  # surfaced to the caller, but not fatal under open


def test_assert_passes_on_match() -> None:
    conn = InMemoryConnector()
    reg = _registry_with_digests({"sql": artifact_digest(conn)})
    assert assert_connector_digests(
        reg, Connectors({"sql": conn}), failure_mode=FailureMode.CLOSED
    ) == []


# --- D. inline enforce path (observe / read) ------------------------------
def _observe(
    reg: Any, *, failure_mode: str, connectors: Any
) -> Any:
    doc = {"agent": "support", "defaults": {"failureMode": failure_mode},
           "allow": [{"observe": ["read"]}]}
    policy = load_policy(doc, reg, schema=load_schema())
    audit = InMemoryAuditSink()
    result = enforce(
        RawCall(resource="Customer", action="read"),
        Actor(id="alice"), Session(id="s1"),
        registry=reg, audit=audit, policy=policy,
        gates=DefaultGateEngine(reg), connectors=connectors,
    )
    return result, audit


def test_enforce_observe_digest_mismatch_closed_denies() -> None:
    cname = _connector_of("Customer", "read")
    reg = _registry_with_digests({cname: BOGUS})
    result, audit = _observe(
        reg, failure_mode="closed", connectors=Connectors({cname: InMemoryConnector()})
    )
    assert result.decision is Decision.DENY
    assert result.rule == DIGEST_MISMATCH
    assert audit.records[-1].decision is Decision.DENY


def test_enforce_observe_digest_mismatch_open_allows() -> None:
    # A read is low-stakes: under failureMode open a digest mismatch is allowed
    # through with no output, exactly like a connector outage (RFC §10).
    cname = _connector_of("Customer", "read")
    reg = _registry_with_digests({cname: BOGUS})
    result, _ = _observe(
        reg, failure_mode="open", connectors=Connectors({cname: InMemoryConnector()})
    )
    assert result.decision is Decision.ALLOW
    assert result.output is None


def test_enforce_observe_digest_match_executes() -> None:
    cname = _connector_of("Customer", "read")
    conn = InMemoryConnector(tables={"Customer": [{"id": 1}]})
    reg = _registry_with_digests({cname: artifact_digest(conn)})
    result, _ = _observe(reg, failure_mode="closed", connectors=Connectors({cname: conn}))
    assert result.decision is Decision.ALLOW
    assert result.output == [{"id": 1}]  # the read actually ran


def test_enforce_observe_no_digest_is_unchanged() -> None:
    cname = _connector_of("Customer", "read")
    conn = InMemoryConnector(tables={"Customer": [{"id": 1}]})
    reg = full_registry()  # no digests declared
    result, _ = _observe(reg, failure_mode="closed", connectors=Connectors({cname: conn}))
    assert result.decision is Decision.ALLOW
    assert result.output == [{"id": 1}]


# --- E. dispatch-worker path (staged effect) ------------------------------
def _stage_effect(store: InMemoryOutboxStore, reg: Any, resource: str, action: str,
                  *, data: dict[str, Any] | None = None,
                  compensation: Compensation | None = None) -> Any:
    resolved = reg.resolve(RawCall(resource=resource, action=action, data=data or {}))
    return store.stage(
        resolved=resolved, actor=Actor(id="alice"), session_id="s1",
        agent="support", state=PendingState.PENDING, compensation=compensation,
    )


def test_worker_digest_mismatch_fails_closed_and_audits() -> None:
    cname = _connector_of("Email", "sendEmail")
    reg = _registry_with_digests({cname: BOGUS})
    audit = InMemoryAuditSink()
    store = InMemoryOutboxStore(audit=audit)
    conn = InMemoryConnector()
    worker = DispatchWorker(store, Connectors({cname: conn}), registry=reg)

    staged = _stage_effect(store, reg, "Email", "sendEmail", data={"to": "x@acme.example"})
    assert worker.drain() == 1

    row = store.get(staged.id)
    assert row is not None
    assert row.state is PendingState.FAILED
    assert row.reason == DIGEST_MISMATCH
    assert conn.effects == []  # the effect never left
    denials = [r for r in audit.records if r.decision is Decision.DENY]
    assert denials and denials[-1].rule == DIGEST_MISMATCH


def test_worker_digest_match_dispatches() -> None:
    cname = _connector_of("Email", "sendEmail")
    conn = InMemoryConnector()
    reg = _registry_with_digests({cname: artifact_digest(conn)})
    store = InMemoryOutboxStore()
    worker = DispatchWorker(store, Connectors({cname: conn}), registry=reg)

    staged = _stage_effect(store, reg, "Email", "sendEmail", data={"to": "x@acme.example"})
    assert worker.drain() == 1
    row = store.get(staged.id)
    assert row is not None and row.state is PendingState.DONE
    assert len(conn.effects) == 1


def test_worker_no_digest_dispatches() -> None:
    cname = _connector_of("Email", "sendEmail")
    conn = InMemoryConnector()
    reg = full_registry()  # no digest pinned
    store = InMemoryOutboxStore()
    worker = DispatchWorker(store, Connectors({cname: conn}), registry=reg)

    staged = _stage_effect(store, reg, "Email", "sendEmail", data={"to": "x@acme.example"})
    assert worker.drain() == 1
    row = store.get(staged.id)
    assert row is not None and row.state is PendingState.DONE
    assert len(conn.effects) == 1


def test_worker_digest_mismatch_does_not_compensate() -> None:
    # A digest mismatch refuses to *call* the connector, so the effect never
    # landed — there is nothing to undo. Unlike a real dispatch FAILED, no
    # compensation is auto-staged (mirrors the scope-lost floor).
    mini = {
        "connectors": {"eff": {"digest": BOGUS}},
        "resources": {"Wire": {"connector": "eff",
                               "actions": {"send": {"kind": "effect",
                                                    "reversibility": "irreversible"}}}},
    }
    reg = load_registry(mini)
    store = InMemoryOutboxStore()
    conn = InMemoryConnector()
    worker = DispatchWorker(store, Connectors({"eff": conn}), registry=reg)

    staged = _stage_effect(
        store, reg, "Wire", "send", data={"amount": 1},
        compensation=Compensation(resource="Wire", action="reverse"),
    )
    assert worker.drain() == 1
    row = store.get(staged.id)
    assert row is not None and row.state is PendingState.FAILED
    assert row.reason == DIGEST_MISMATCH
    # no compensation row was staged
    assert store.list_by_state(PendingState.PENDING) == []
    assert conn.effects == []


# --- F. the low-level helper the pipeline/worker share --------------------
def test_pinned_connector_mismatch_helper() -> None:
    conn = InMemoryConnector()
    reg_ok = _registry_with_digests({_connector_of("Email", "sendEmail"): artifact_digest(conn)})
    reg_bad = _registry_with_digests({_connector_of("Email", "sendEmail"): BOGUS})
    cname = _connector_of("Email", "sendEmail")
    ok = reg_ok.resolve(RawCall(resource="Email", action="sendEmail"))
    bad = reg_bad.resolve(RawCall(resource="Email", action="sendEmail"))
    plain = full_registry().resolve(RawCall(resource="Email", action="sendEmail"))
    conns = Connectors({cname: conn})
    assert pinned_connector_mismatch(conns, ok) is False
    assert pinned_connector_mismatch(conns, bad) is True
    assert pinned_connector_mismatch(conns, plain) is False  # nothing pinned
    # unknown connector ⇒ defer to the normal fail-closed execute path
    assert pinned_connector_mismatch(Connectors({}), bad) is False
