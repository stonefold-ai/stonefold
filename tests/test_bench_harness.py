"""Benchmark harness (docs/15) — the machinery is tested; no result is produced.

Every test drives the deterministic fake LLM, so it validates that the harness *runs*
and *scores correctly* (S0 lets the attack through, S3 stops it), never a measurement.
"""

from __future__ import annotations

from pathlib import Path

from acp_bench.attacks import ATTACKS, ATTACKER_IBAN, INVITE_WIRE
from acp_bench.conditions import CONDITIONS, Condition, Rung, build_arena, is_configured
from acp_bench.harness import run_security, unconfigured_rungs, unwired_attacks
from acp_bench.matrix import aggregate
from acp_bench.model import MeteredProvider, ModelSpec, build_provider, model_by_key
from acp_bench.raw_log import read_jsonl, write_jsonl
from acp_bench.report import ReportMeta, render
from acp_bench.runner import (
    BENIGN_LABEL,
    run_attack_trial,
    run_benign_trial,
)
from acp_bench.tracks import (
    TOOL_COUNTS,
    capability_set,
    mcp_surface,
    retrieval_surface,
    score_reliability,
    sif_surface,
)

FAKE = model_by_key("fake")
S0 = CONDITIONS[0]
S3 = CONDITIONS[3]


# --- model + token metering -----------------------------------------------
def test_pinned_models_include_a_small_tier() -> None:
    keys = {m.key for m in [model_by_key("fake"), model_by_key("small")]}
    assert {"fake", "small"} <= keys
    assert build_provider(FAKE).label == "fake:scripted"


def test_metered_provider_counts_tokens() -> None:
    provider = MeteredProvider(build_provider(FAKE))
    turn = provider.complete("sys", [{"role": "user", "content": "hello"}], [])
    assert isinstance(turn.text, str)
    assert provider.meter.total > 0 and provider.meter.calls == 1


# --- conditions -----------------------------------------------------------
def test_rung_configuration_status() -> None:
    status = {c.rung: is_configured(c) for c in CONDITIONS}
    assert status[Rung.S0] is True and status[Rung.S3] is True
    assert status[Rung.S1] is False and status[Rung.S2] is False  # author-owned


def test_build_arena_s0_and_s3() -> None:
    assert build_arena(S0).condition.rung is Rung.S0
    assert build_arena(S3).condition.rung is Rung.S3


def test_unconfigured_rung_raises() -> None:
    s1 = CONDITIONS[1]
    try:
        build_arena(s1)
    except ValueError as exc:
        assert "UNCONFIGURED" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected UNCONFIGURED to raise")


# --- attacks --------------------------------------------------------------
def test_attack_taxonomy_is_a1_through_a7() -> None:
    assert [a.cls for a in ATTACKS] == ["A1", "A2", "A3", "A4", "A5", "A6", "A7"]
    assert INVITE_WIRE.wired and INVITE_WIRE.cls == "A2"
    assert [a.cls for a in ATTACKS if not a.wired] == ["A1", "A3", "A4", "A5", "A6", "A7"]


def test_invite_wire_signature() -> None:
    assert INVITE_WIRE.landed({"iban": ATTACKER_IBAN}) is True
    assert INVITE_WIRE.landed({"iban": "GB29ACME0000011111"}) is False


# --- runner: the core discrimination --------------------------------------
def test_attack_executes_at_s0_but_not_s3() -> None:
    at_s0 = run_attack_trial(FAKE, S0, INVITE_WIRE, rep=0)
    at_s3 = run_attack_trial(FAKE, S3, INVITE_WIRE, rep=0)
    # attempted at both rungs (the agent obeys the injection either way)…
    assert at_s0.attempted is True and at_s3.attempted is True
    # …but the unauthorized effect only *executes* without the gateway (§3).
    assert at_s0.executed is True
    assert at_s3.executed is False
    assert at_s0.tokens > 0 and at_s3.tokens > 0


def test_benign_task_succeeds_at_both_rungs() -> None:
    assert run_benign_trial(FAKE, S0, rep=0).benign_ok is True
    assert run_benign_trial(FAKE, S3, rep=0).benign_ok is True


# --- matrix + report ------------------------------------------------------
def test_matrix_and_report_end_to_end() -> None:
    matrix, trials = run_security(
        (FAKE,), (S0, S3), (INVITE_WIRE,), reps=2
    )
    a2_s0 = matrix.cell("A2", "S0")
    a2_s3 = matrix.cell("A2", "S3")
    assert a2_s0 is not None and a2_s0.asr_executed == 1.0
    assert a2_s3 is not None and a2_s3.asr_executed == 0.0 and a2_s3.asr_attempted == 1.0
    assert matrix.bts["S0"] == 1.0 and matrix.bts["S3"] == 1.0
    # every benign + attack cell ran `reps` times
    assert len([t for t in trials if t.scenario == BENIGN_LABEL and t.rung == "S3"]) == 2

    report = render(matrix, ReportMeta(smoke=True, models=("fake",), reps=2,
                                       unconfigured_rungs=("S1", "S2")))
    assert "SMOKE TEST" in report and "A2" in report and "S1, S2" in report


def test_aggregate_reports_variance_fields() -> None:
    _, trials = run_security((FAKE,), (S3,), (INVITE_WIRE,), reps=3)
    cell = aggregate(trials).cell("A2", "S3")
    assert cell is not None and cell.n == 3 and cell.tokens_std >= 0.0


# --- raw log round-trip ---------------------------------------------------
def test_raw_log_roundtrip(tmp_path: Path) -> None:
    _, trials = run_security((FAKE,), (S0,), (INVITE_WIRE,), reps=1)
    path = write_jsonl(tmp_path / "trials.jsonl", trials)
    rows = read_jsonl(path)
    assert len(rows) == len(trials)
    assert rows[0]["model"] == "fake" and "executed" in rows[0]


# --- harness bookkeeping --------------------------------------------------
def test_unconfigured_and_unwired_are_surfaced() -> None:
    assert unconfigured_rungs(CONDITIONS) == ("S1", "S2")
    assert unwired_attacks(ATTACKS) == ("A1", "A3", "A4", "A5", "A6", "A7")


# --- Track R surfaces + scorer --------------------------------------------
def test_track_r_surfaces_have_capability_parity() -> None:
    assert TOOL_COUNTS == (1, 10, 30, 70, 100)
    caps = capability_set(30)
    assert len(caps) == 30
    assert len(mcp_surface(caps)) == 30            # N tools
    sif = sif_surface(caps)
    assert len(sif) == 1                            # one submit_intent…
    assert len(sif[0].input_schema["properties"]["resource"]["enum"]) == 30  # …N enum'd
    assert len(retrieval_surface(caps, "read res_7", k=10)) == 10  # mandatory baseline


def test_reliability_scorer() -> None:
    from acp_ap_demo.agent import AgentResult, AgentStep

    result = AgentResult(final_text="", steps=[
        AgentStep(tool="submit_intent", args={"resource": "R", "action": "read"}, result={}),
        AgentStep(tool="submit_intent", args={"action": "read"}, result={}),       # malformed
        AgentStep(tool="hallucinated_tool", args={}, result={}),                   # not declared
    ])
    score = score_reliability(result, declared_names={"submit_intent"})
    assert score.total_calls == 3
    assert score.hallucinated == 1 and score.malformed == 1


# --- the CLI smoke path exits clean ---------------------------------------
def test_cli_smoke_runs() -> None:
    from acp_bench.__main__ import main
    assert main(["--smoke"]) == 0


def test_model_spec_label() -> None:
    assert ModelSpec(key="x", provider="fake").label == "x"


# --- Track R reliability runner -------------------------------------------
def test_reliability_surfaces_keep_capability_parity() -> None:
    from acp_bench.reliability import MCP, MCP_RETRIEVAL, PROBES, SIF, RETRIEVAL_K, surface_for

    probe = PROBES[1]  # pay-invoice
    mcp = surface_for(MCP, 30, probe)
    assert len(mcp.tools) == 30 and probe.mcp_tool in mcp.tool_names
    sif = surface_for(SIF, 30, probe)
    assert len(sif.tools) == 1 and len(sif.resources) == 30 and probe.resource in sif.resources
    retr = surface_for(MCP_RETRIEVAL, 30, probe)
    assert len(retr.tools) == RETRIEVAL_K


def test_reliability_scoring() -> None:
    from acp_ap_demo.llm import ToolCall
    from acp_bench.reliability import (
        CORRECT, HALLUCINATED, MALFORMED, MCP, PROBES, SIF, WRONG_TOOL, _score, surface_for,
    )

    probe = PROBES[0]  # account-balance -> read_account / Account.read
    mcp = surface_for(MCP, 10, probe)
    assert _score(MCP, mcp, probe, ToolCall("1", "read_account", {}))[0] == CORRECT
    assert _score(MCP, mcp, probe, ToolCall("1", "send_email", {}))[0] == WRONG_TOOL
    assert _score(MCP, mcp, probe, ToolCall("1", "nope_tool", {}))[0] == HALLUCINATED
    assert _score(MCP, mcp, probe, None)[0] == "no_call"

    sif = surface_for(SIF, 10, probe)
    good = ToolCall("1", "submit_intent", {"resource": "Account", "action": "read"})
    assert _score(SIF, sif, probe, good)[0] == CORRECT
    assert _score(SIF, sif, probe, ToolCall("1", "submit_intent", {"resource": "Account"}))[0] == MALFORMED
    bad = ToolCall("1", "submit_intent", {"resource": "Nonexistent", "action": "read"})
    assert _score(SIF, sif, probe, bad)[0] == HALLUCINATED


def test_reliability_matrix_and_runner_mechanics() -> None:
    from acp_bench.reliability import MCP, SIF, reliability_matrix, run_reliability

    trials = run_reliability((FAKE,), (10,), conditions=(MCP, SIF), reps=1)
    assert len(trials) == 2 * len(__import__("acp_bench.reliability", fromlist=["PROBES"]).PROBES)
    cells = reliability_matrix(trials)
    assert {c.condition for c in cells} == {MCP, SIF}
    assert all(0.0 <= c.correct <= 1.0 for c in cells)
