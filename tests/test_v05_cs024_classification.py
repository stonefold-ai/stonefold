"""v0.5 CS-024 — classification ordering for ``disclosure`` (RFC §7.12; registry §4).

``disclosure.maxClassification`` compares by the classification set's DECLARED
order: the built-in ``resultSensitivity`` values are ordered ``public < internal
< confidential < restricted``; a domain substituting its own labels declares
them as an ordered value set (order is list position, lowest first). A value
missing from the declared order makes the gate fail closed (RFC §8).
"""

from __future__ import annotations

from typing import Any

from stonefold_core import Actor, load_registry
from stonefold_core.enums import Outcome
from stonefold_gates.gates import disclosure
from tests.conftest import gate_ctx, load_yaml, FIXTURES


def _registry_doc(**overrides: Any) -> dict[str, Any]:
    doc: dict[str, Any] = load_yaml(FIXTURES / "registry_min.yaml")
    doc.setdefault("resources", {})["Chart"] = {
        "connector": "in_memory",
        "actions": {
            "read": {"kind": "observe", "resultSensitivity": "confidential"},
            "readSealed": {"kind": "observe", "resultSensitivity": "restricted"},
            "readOdd": {"kind": "observe", "resultSensitivity": "weird-label"},
        },
    }
    doc.update(overrides)
    return doc


def _run(action: str, cfg: dict[str, Any], *, actor: Actor | None = None,
         registry_doc: dict[str, Any] | None = None) -> Any:
    reg = load_registry(registry_doc or _registry_doc())
    return disclosure(cfg, gate_ctx("Chart", action, registry=reg, actor=actor))


# --- the built-in order ------------------------------------------------------
def test_sensitivity_at_or_below_the_ceiling_passes() -> None:
    assert _run("read", {"maxClassification": "confidential"}).outcome is Outcome.PASS
    assert _run("read", {"maxClassification": "restricted"}).outcome is Outcome.PASS


def test_sensitivity_above_the_ceiling_fails() -> None:
    r = _run("readSealed", {"maxClassification": "confidential"})
    assert r.outcome is Outcome.FAIL
    assert "exceeds" in r.reason


# --- fail closed on labels outside the declared order ------------------------
def test_sensitivity_missing_from_the_order_fails_closed() -> None:
    r = _run("readOdd", {"maxClassification": "restricted"})
    assert r.outcome is Outcome.FAIL
    assert "not in the declared order" in r.reason


def test_unresolvable_ceiling_fails_closed() -> None:
    r = _run("read", {"maxClassification": "actor.no_such_claim"})
    assert r.outcome is Outcome.FAIL
    assert "fail-closed" in r.reason


# --- the condition-path ceiling (the track-operator fixture's form) -----------
def test_ceiling_from_actor_clearance_path() -> None:
    cleared = Actor(id="op1", claims={"clearance": "restricted"})
    assert (
        _run("readSealed", {"maxClassification": "actor.clearance"}, actor=cleared)
        .outcome is Outcome.PASS
    )
    limited = Actor(id="op2", claims={"clearance": "internal"})
    assert (
        _run("readSealed", {"maxClassification": "actor.clearance"}, actor=limited)
        .outcome is Outcome.FAIL
    )


# --- a domain's substituted, ordered labels (CS-024) --------------------------
def test_domain_declared_order_is_honoured() -> None:
    doc = _registry_doc(classifications=["green", "amber", "red"])
    doc["resources"]["Chart"]["actions"]["read"]["resultSensitivity"] = "amber"
    assert (
        _run("read", {"maxClassification": "red"}, registry_doc=doc).outcome
        is Outcome.PASS
    )
    r = _run("read", {"maxClassification": "green"}, registry_doc=doc)
    assert r.outcome is Outcome.FAIL
    # with substituted labels the built-ins are OUT of the order ⇒ fail closed
    # (not a declared label, and not resolvable as a condition path either)
    r2 = _run("read", {"maxClassification": "restricted"}, registry_doc=doc)
    assert r2.outcome is Outcome.FAIL and "fail-closed" in r2.reason


# --- the sink check still composes ------------------------------------------
def test_classification_passes_then_sink_check_applies() -> None:
    r = _run("read", {"maxClassification": "restricted", "allowSink": ["vault"]})
    assert r.outcome is Outcome.FAIL and "allowSink" in r.reason
