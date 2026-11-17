"""Research execution contract: uncertainty never authorizes another effect."""
from datetime import timedelta
from typing import Any

from stonefold_connectors import InMemoryConnector
from stonefold_core import Actor, PendingState
from stonefold_store import DispatchWorker
from tests.test_m4_outbox import harness


class EffectThenTimeout(InMemoryConnector):
    def dispatch(self, action: Any, actor: Any, idempotency_key: str) -> Any:
        super().dispatch(action, actor, idempotency_key)
        raise TimeoutError("response lost after effect")


def test_effect_then_timeout_is_unknown_and_never_compensated() -> None:
    h = harness({"agent": "rx", "allow": [{"effect": ["prescribe"]}]},
                connector=EffectThenTimeout())
    result = h.enforce("Prescribing", "prescribe", Actor(id="alice"), {"drug": "X"})
    assert h.worker.drain() == 1
    assert h.get(result.ticket).state.value == "outcome_unknown"
    assert len(h.effect_conn.effects) == 1
    assert h.outbox.list_by_state(PendingState.PENDING) == []
    assert h.audit.records[-1].outcome == "unknown"
    assert h.worker.drain() == 0

    # The extra uncertainty event must not become a second policy decision.
    from stonefold_core.enums import EnforcementMode
    from stonefold_report import build_report, render
    from stonefold_report.html import render_html
    report = build_report([
        r.model_copy(update={"enforcement": EnforcementMode.ADVISORY}) for r in h.audit.records
    ], agent="rx")
    assert report.coverage.observed == 1
    assert report.outcomes.unknown == 1
    assert "unknown outcome" in render(report)
    assert "unknown outcome" in render_html(report)


def test_restart_recovers_expired_claim_but_does_not_resend() -> None:
    h = harness({"agent": "support", "allow": [{"effect": ["sendEmail"]}]})
    result = h.enforce("Email", "sendEmail", Actor(id="alice"))
    claimed = h.outbox.claim_next_pending()
    assert claimed is not None
    # A new worker before the deadline must leave a live claim alone.
    worker = DispatchWorker(h.outbox, h.connectors, clock=lambda: claimed.updated_at)
    assert worker.drain() == 0
    assert h.get(result.ticket).state is PendingState.DISPATCHING
    worker = DispatchWorker(h.outbox, h.connectors,
                            clock=lambda: claimed.updated_at + timedelta(minutes=6))
    assert worker.drain() == 0
    assert h.get(result.ticket).state.value == "outcome_unknown"
    assert h.effect_conn.effects == []
    assert h.audit.records[-1].outcome == "unknown"
    count = len(h.audit.records)
    assert worker.drain() == 0
    assert len(h.audit.records) == count


def test_late_confirmed_success_resolves_recovered_claim_without_resend() -> None:
    h = harness({"agent": "support", "allow": [{"effect": ["sendEmail"]}]})
    result = h.enforce("Email", "sendEmail", Actor(id="alice"))

    class SlowSuccess(InMemoryConnector):
        def dispatch(self, action: Any, actor: Any, idempotency_key: str) -> Any:
            receipt = super().dispatch(action, actor, idempotency_key)
            claimed = h.get(result.ticket)
            h.outbox.recover_dispatching(claimed.updated_at + timedelta(minutes=6))
            assert h.get(result.ticket).state.value == "outcome_unknown"
            return receipt

    effect = SlowSuccess()
    from stonefold_core import Connectors
    worker = DispatchWorker(h.outbox, Connectors({"email": effect}))
    assert worker.drain() == 1
    assert h.get(result.ticket).state is PendingState.DONE
    assert len(effect.effects) == 1
    assert [r.outcome for r in h.audit.records[-2:]] == ["unknown", "success"]


def test_unknown_outcome_does_not_release_or_consume_reservation() -> None:
    from tests.test_v03_reservation import harness as reservation_harness, T0
    h = reservation_harness()
    result = h.enforce()
    worker = h.worker_at(T0 + timedelta(minutes=1), connector=EffectThenTimeout())
    assert worker.run_once()
    assert h.row(result.ticket).state is PendingState.OUTCOME_UNKNOWN
    assert h.line_state() == "reserved"
    assert worker.release_terminal_claims() == 0
    assert h.audit.records[-1].consumption is None
