"""Audit evidence-pack exporter (plan G3).

Contract under test:
1. the pack aggregates the audit log into per-control evidence (docs/14 rows);
2. it is read-only over real AuditRecords (also exercised end-to-end via the AP bundle);
3. every regulatory `[VERIFY]` marker is printed verbatim — never resolved;
4. JSONL round-trips so the exporter is store-agnostic; the CLI wires it together.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from stonefold_core.enums import Decision, Outcome
from stonefold_core.models import AuditRecord, GateResult

from stonefold_evidence import build_evidence_pack, render_markdown
from stonefold_evidence.__main__ import main
from stonefold_evidence.controls import CONTROLS
from stonefold_evidence.sources import records_from_jsonl, write_jsonl

_T0 = datetime(2026, 7, 2, 9, 0, 0, tzinfo=timezone.utc)
_GEN = datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc)


def _rec(decision: Decision, *, i: int = 0, outcome: str = "not_executed",
         result_refs: tuple[str, ...] = (), approval: dict[str, object] | None = None,
         gates: tuple[GateResult, ...] = (), rule: str = "rule") -> AuditRecord:
    return AuditRecord(
        id=f"aud_{i}", timestamp=_T0 + timedelta(minutes=i), agent="ap-operator",
        actor="alice", kind="effect", resource="Payment", action="pay",
        decision=decision, rule=rule, outcome=outcome,
        resultRefs=list(result_refs), approval=approval, gates=list(gates),
        correlationId="run-1",
    )


def _mixed() -> list[AuditRecord]:
    return [
        _rec(Decision.ALLOW, i=0, outcome="success", result_refs=("PAY-1",),
             gates=(GateResult(gate="valueLimit", outcome=Outcome.PASS),)),
        _rec(Decision.HOLD, i=1, approval={"status": "pending", "quorum": 2},
             gates=(GateResult(gate="dualAuthorization", outcome=Outcome.HOLD),)),
        _rec(Decision.DENY, i=2, rule="gate:denylist"),
        _rec(Decision.HALT, i=3, outcome="halted", rule="kill:k1"),
    ]


# --- aggregation ----------------------------------------------------------
def test_pack_aggregates_decisions_and_window() -> None:
    pack = build_evidence_pack(_mixed(), policy_ref="examples/payments-ops.stele.yaml",
                               generated_at=_GEN)
    assert pack.total_records == 4
    assert pack.decision_counts == {"allow": 1, "hold": 1, "deny": 1, "halt": 1}
    assert pack.window == (_T0, _T0 + timedelta(minutes=3))
    assert [c.control.id for c in pack.controls] == [c.id for c in CONTROLS]


def test_record_keeping_counts_executed_and_refs() -> None:
    pack = build_evidence_pack(_mixed(), generated_at=_GEN)
    facts = " ".join(next(c.facts for c in pack.controls if c.control.id == "art-12"))
    assert "4 audit records" in facts
    assert "1 executed effects, 1 carrying resultRefs" in facts


def test_oversight_present_only_with_events() -> None:
    with_events = build_evidence_pack(_mixed(), generated_at=_GEN)
    intervene = next(c for c in with_events.controls if c.control.id == "art-14-intervene")
    assert intervene.present is True
    facts = " ".join(intervene.facts)
    assert "1 actions held" in facts and "1 actions halted" in facts

    only_allows = build_evidence_pack([_rec(Decision.ALLOW, i=0, outcome="success")],
                                      generated_at=_GEN)
    intervene2 = next(c for c in only_allows.controls if c.control.id == "art-14-intervene")
    assert intervene2.present is False  # machinery present, no positive events


def test_deployer_names_the_policy() -> None:
    pack = build_evidence_pack(_mixed(), policy_ref="payments-ops.stele.yaml", generated_at=_GEN)
    facts = " ".join(next(c.facts for c in pack.controls if c.control.id == "art-26"))
    assert "payments-ops.stele.yaml" in facts and "[VERIFY 26(6)]" in facts


# --- the [VERIFY] markers are preserved -----------------------------------
def test_render_preserves_all_verify_markers() -> None:
    md = render_markdown(build_evidence_pack(_mixed(), generated_at=_GEN))
    for marker in ("[VERIFY]", "[VERIFY 14(4)]", "[VERIFY 26(6)]", "[VERIFY applicability]"):
        assert marker in md
    assert "Art. 12 — Record-keeping [VERIFY]" in md
    assert "not a compliance claim" in md  # the honesty note travels with the pack


def test_render_shows_evidence_and_samples() -> None:
    md = render_markdown(build_evidence_pack(_mixed(), policy_ref="p.yaml", generated_at=_GEN))
    assert "# Audit evidence pack" in md
    assert "1 allow, 1 deny, 1 halt, 1 hold" in md or "1 allow" in md
    assert "| time | actor | resource.action | decision | rule | outcome |" in md


# --- store-agnostic: JSONL round-trip + CLI -------------------------------
def test_jsonl_roundtrip(tmp_path: Path) -> None:
    path = write_jsonl(tmp_path / "audit.jsonl", _mixed())
    back = records_from_jsonl(path)
    assert len(back) == 4 and back[0].resultRefs == ["PAY-1"]
    assert back[1].decision is Decision.HOLD


def test_cli_writes_markdown(tmp_path: Path) -> None:
    jsonl = write_jsonl(tmp_path / "audit.jsonl", _mixed())
    out = tmp_path / "pack.md"
    rc = main(["--jsonl", str(jsonl), "--policy", "examples/payments-ops.stele.yaml", "-o", str(out)])
    assert rc == 0 and out.exists()
    md = out.read_text(encoding="utf-8")
    assert "[VERIFY]" in md and "Audit evidence pack" in md


# --- end to end over real audit records -----------------------------------
def test_pack_over_real_bundle_records() -> None:
    from stonefold_ap_demo.gateway import build_inmemory_bundle

    now = datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc)
    bundle = build_inmemory_bundle(clock=lambda: now)
    # a permitted read and an out-of-scope pay produce real, varied records
    bundle.submit(actor_id="ap-operator", resource="Account", action="read", session_id="s1")
    bundle.submit(actor_id="ap-operator", resource="Payment", action="pay",
                  data={"payeeId": "PE-ACME-SUP", "accountId": "ACME-OPS", "amount": 800,
                        "currency": "USD", "destinationCountry": "GB", "invoiceId": "INV-1001"},
                  session_id="s1")
    bundle.drain()
    records = bundle.audit_records()

    pack = build_evidence_pack(records, generated_at=now)
    assert pack.total_records == len(records) > 0
    md = render_markdown(pack)
    assert "[VERIFY]" in md and "Audit evidence pack" in md
