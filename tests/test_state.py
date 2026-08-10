from __future__ import annotations

import time

import pytest

from hookdeck.state import STATUS_RUNNING, DeliveryLedger


@pytest.fixture()
def ledger(tmp_path):
    led = DeliveryLedger(tmp_path / "state.db")
    yield led
    led.close()


def test_first_delivery_is_admitted(ledger):
    result = ledger.admit("evt_1", route="github", attempt=1)
    assert result.admitted
    assert result.agent_attempts == 1


def test_repeat_of_the_same_attempt_is_a_duplicate(ledger):
    ledger.admit("evt_1", route="github", attempt=1)
    result = ledger.admit("evt_1", route="github", attempt=1)
    assert not result.admitted
    assert "duplicate" in result.reason


def test_a_higher_attempt_number_is_admitted(ledger):
    # This is the case that makes retry work at all: the adapter asks Hookdeck
    # to redeliver the same event id, and the redelivery must not be deduped.
    ledger.admit("evt_1", route="github", attempt=1)
    ledger.mark_failed("evt_1", "boom")
    result = ledger.admit("evt_1", route="github", attempt=2)
    assert result.admitted
    assert result.agent_attempts == 2


def test_unknown_attempt_number_is_admitted_only_after_a_failure(ledger):
    ledger.admit("evt_1", route="github", attempt=0)
    assert not ledger.admit("evt_1", route="github", attempt=0).admitted

    ledger.mark_failed("evt_1", "boom")
    assert ledger.admit("evt_1", route="github", attempt=0).admitted


def test_success_is_terminal_for_an_unnumbered_redelivery(ledger):
    ledger.admit("evt_1", route="github", attempt=0)
    ledger.mark_succeeded("evt_1")
    assert not ledger.admit("evt_1", route="github", attempt=0).admitted


def test_dedup_survives_a_restart(ledger, tmp_path):
    ledger.admit("evt_1", route="github", attempt=1)
    ledger.close()

    reopened = DeliveryLedger(tmp_path / "state.db")
    try:
        assert not reopened.admit("evt_1", route="github", attempt=1).admitted
    finally:
        reopened.close()


def test_counts_and_recent_failures(ledger):
    ledger.admit("evt_ok", route="a", attempt=1)
    ledger.mark_succeeded("evt_ok")
    ledger.admit("evt_bad", route="b", attempt=1)
    ledger.mark_exhausted("evt_bad", "failure")

    counts = ledger.counts()
    assert counts["succeeded"] == 1
    assert counts["exhausted"] == 1
    assert [row["event_id"] for row in ledger.recent_failures()] == ["evt_bad"]


def test_prune_drops_terminal_rows_but_keeps_running_ones(ledger):
    ledger.admit("evt_running", route="a", attempt=1)
    ledger.admit("evt_done", route="a", attempt=1)
    ledger.mark_succeeded("evt_done")

    # Age every row past the TTL.
    with ledger._lock:
        ledger._conn.execute("UPDATE deliveries SET updated_at = ?", (time.time() - 10_000,))
        ledger._conn.commit()

    assert ledger.prune(3600) == 1
    assert ledger.get("evt_done") is None
    assert ledger.get("evt_running")["status"] == STATUS_RUNNING


def test_stale_running_reports_lost_runs(ledger):
    ledger.admit("evt_lost", route="a", attempt=1)
    with ledger._lock:
        ledger._conn.execute("UPDATE deliveries SET updated_at = ?", (time.time() - 10_000,))
        ledger._conn.commit()

    assert [row["event_id"] for row in ledger.stale_running(3600)] == ["evt_lost"]


def test_an_operator_replay_reaches_an_exhausted_event(ledger):
    # Every documented recovery path — `hermes hookdeck replay`, the dashboard
    # button, the agent's retry tool — arrives as a MANUAL redelivery of the
    # same event id. Refusing those would answer 200 and do nothing: a green
    # tick over a dead event.
    ledger.admit("evt_spent", route="a", attempt=1)
    ledger.mark_exhausted("evt_spent", "failure")

    assert ledger.rejection_reason("evt_spent", 2) is not None
    assert ledger.rejection_reason("evt_spent", 2, operator_initiated=True) is None

    admitted = ledger.admit("evt_spent", route="a", attempt=2, operator_initiated=True)
    assert admitted.admitted
    # Budget reset: otherwise the replay buys one attempt, not a retry.
    assert admitted.agent_attempts == 1


def test_hookdecks_own_retries_still_stop_at_exhausted(ledger):
    ledger.admit("evt_spent", route="a", attempt=1)
    ledger.mark_exhausted("evt_spent", "failure")
    assert "budget" in (ledger.rejection_reason("evt_spent", 5) or "")


def test_a_late_completion_cannot_overwrite_a_newer_run(ledger):
    # After a sweep timeout the original run can finish while its redelivery is
    # already in flight; marking the row terminal then would hide live work
    # from boot recovery.
    ledger.admit("evt_x", route="a", attempt=1, session_chat_id="run-1")
    ledger.admit("evt_x", route="a", attempt=2, session_chat_id="run-2")

    ledger.mark_succeeded("evt_x", session_chat_id="run-1")
    assert ledger.get("evt_x")["status"] == "running"

    ledger.mark_succeeded("evt_x", session_chat_id="run-2")
    assert ledger.get("evt_x")["status"] == "succeeded"


def test_scheduled_resumes_survive_the_process_that_set_them(ledger, tmp_path):
    import time as _time

    ledger.schedule_resume("web_1", "mine", _time.time() - 1)
    ledger.schedule_resume("web_2", "later", _time.time() + 3600)
    ledger.close()

    # The restart a pause most often precedes.
    reopened = DeliveryLedger(tmp_path / "state.db")
    try:
        due = [row["connection_id"] for row in reopened.due_resumes()]
        assert due == ["web_1"]
        reopened.cancel_scheduled_resume("web_1")
        assert reopened.due_resumes() == []
    finally:
        reopened.close()
