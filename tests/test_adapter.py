"""End-to-end tests of the ingest path against the stubbed Hermes internals."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from hookdeck.adapter import HookdeckAdapter
from hookdeck.state import DeliveryLedger
from hookdeck.verify import compute_signature
from tests.hermes_stub import PlatformConfig, ProcessingOutcome, SendResult

SECRET = "whsec_test"


class FakeAPI:
    """Records the retry calls the adapter makes on a failed run."""

    def __init__(self) -> None:
        self.retried: list[str] = []
        self.fail_with: Exception | None = None

    async def retry_event(self, event_id: str) -> Any:
        if self.fail_with:
            raise self.fail_with
        self.retried.append(event_id)
        return {"status": "queued"}

    async def aclose(self) -> None:
        return None


def make_adapter(tmp_path, routes: dict, **extra) -> HookdeckAdapter:
    config = PlatformConfig(
        extra={
            "mode": "push",
            "host": "127.0.0.1",
            "secret": SECRET,
            "routes": routes,
            "state_path": str(tmp_path / "state.db"),
            **extra,
        }
    )
    adapter = HookdeckAdapter(config)
    adapter._validate_startup()
    adapter._ledger = DeliveryLedger(tmp_path / "state.db")
    adapter._api = FakeAPI()
    return adapter


async def post(
    client: TestClient,
    body: dict,
    *,
    path: str = "/hookdeck",
    event_id: str = "evt_1",
    attempt: int = 1,
    source: str = "",
    secret: str = SECRET,
    sign: bool = True,
    extra_headers: dict | None = None,
):
    raw = json.dumps(body).encode()
    headers = {
        "content-type": "application/json",
        "x-hookdeck-eventid": event_id,
        "x-hookdeck-attempt-count": str(attempt),
    }
    if source:
        headers["x-hookdeck-source-name"] = source
    if sign:
        headers["x-hookdeck-signature"] = compute_signature(raw, secret)
    headers.update(extra_headers or {})
    return await client.post(path, data=raw, headers=headers)


@pytest.fixture()
async def client_factory(tmp_path):
    created: list[TestClient] = []
    adapters: list[HookdeckAdapter] = []

    async def _make(routes: dict, **extra):
        adapter = make_adapter(tmp_path, routes, **extra)
        client = TestClient(TestServer(adapter.build_app()))
        await client.start_server()
        created.append(client)
        adapters.append(adapter)
        return adapter, client

    yield _make

    for client in created:
        await client.close()
    for adapter in adapters:
        if adapter._ledger:
            adapter._ledger.close()


async def _settle() -> None:
    """Let fire-and-forget dispatch tasks run to completion."""
    for _ in range(20):
        await asyncio.sleep(0)


# ----------------------------------------------------------------------
# Verification and routing
# ----------------------------------------------------------------------


async def test_unsigned_delivery_is_rejected(client_factory):
    _adapter, client = await client_factory({"default": {}})
    response = await post(client, {"hello": "world"}, sign=False)
    assert response.status == 401


async def test_delivery_signed_with_the_wrong_secret_is_rejected(client_factory):
    _adapter, client = await client_factory({"default": {}})
    response = await post(client, {"hello": "world"}, secret="not-the-secret")
    assert response.status == 401


async def test_unmatched_source_returns_404(client_factory):
    _adapter, client = await client_factory(
        {"github": {"source": "github"}, "stripe": {"source": "stripe"}}
    )
    response = await post(client, {"hi": 1}, source="shopify")
    assert response.status == 404


async def test_route_is_resolved_from_the_hookdeck_source_name(client_factory):
    adapter, client = await client_factory(
        {"github-prs": {"source": "github"}, "stripe": {"source": "stripe"}}
    )
    seen: list[Any] = []
    adapter.run_agent = lambda event: _record(seen, event)

    response = await post(client, {"hi": 1}, source="github")
    assert response.status == 202
    await _settle()
    assert seen[0].metadata["hookdeck_route"] == "github-prs"


async def test_explicit_path_segment_wins(client_factory):
    adapter, client = await client_factory({"a": {"source": "sa"}, "b": {"source": "sb"}})
    seen: list[Any] = []
    adapter.run_agent = lambda event: _record(seen, event)

    response = await post(client, {"hi": 1}, path="/hookdeck/b", source="sa")
    assert response.status == 202
    await _settle()
    assert seen[0].metadata["hookdeck_route"] == "b"


async def _record(sink: list, event) -> ProcessingOutcome:
    sink.append(event)
    return ProcessingOutcome.SUCCESS


# ----------------------------------------------------------------------
# Filtering
# ----------------------------------------------------------------------


async def test_event_type_outside_the_allowlist_is_ignored(client_factory):
    adapter, client = await client_factory({"default": {"events": ["charge.succeeded"]}})
    seen: list[Any] = []
    adapter.run_agent = lambda event: _record(seen, event)

    response = await post(client, {"type": "charge.refunded"})
    assert response.status == 200
    assert (await response.json())["status"] == "ignored"
    await _settle()
    assert seen == []


async def test_event_type_is_read_from_a_configured_body_path(client_factory):
    adapter, client = await client_factory(
        {"default": {"events": ["dispute"], "event_path": "data.kind"}}
    )
    seen: list[Any] = []
    adapter.run_agent = lambda event: _record(seen, event)

    assert (await post(client, {"data": {"kind": "dispute"}})).status == 202
    await _settle()
    assert len(seen) == 1


async def test_provider_headers_still_drive_event_detection(client_factory):
    adapter, client = await client_factory({"default": {"events": ["pull_request"]}})
    seen: list[Any] = []
    adapter.run_agent = lambda event: _record(seen, event)

    response = await post(
        client, {"action": "opened"}, extra_headers={"X-GitHub-Event": "pull_request"}
    )
    assert response.status == 202
    await _settle()
    assert len(seen) == 1


# ----------------------------------------------------------------------
# Deduplication
# ----------------------------------------------------------------------


async def test_a_repeated_delivery_runs_the_agent_once(client_factory):
    adapter, client = await client_factory({"default": {}})
    seen: list[Any] = []
    adapter.run_agent = lambda event: _record(seen, event)

    assert (await post(client, {"n": 1}, event_id="evt_dup", attempt=1)).status == 202
    await _settle()
    second = await post(client, {"n": 1}, event_id="evt_dup", attempt=1)
    assert second.status == 200
    assert (await second.json())["status"] == "duplicate"
    await _settle()
    assert len(seen) == 1


async def test_a_redelivery_with_a_higher_attempt_runs_again(client_factory):
    adapter, client = await client_factory({"default": {}})
    seen: list[Any] = []
    adapter.run_agent = lambda event: _record(seen, event)

    await post(client, {"n": 1}, event_id="evt_retry", attempt=1)
    await _settle()
    assert (await post(client, {"n": 1}, event_id="evt_retry", attempt=2)).status == 202
    await _settle()
    assert len(seen) == 2


# ----------------------------------------------------------------------
# Admission control
# ----------------------------------------------------------------------


async def test_events_over_the_concurrency_limit_are_deferred_not_dropped(client_factory):
    adapter, client = await client_factory({"default": {}}, max_concurrent=1)
    gate = asyncio.Event()

    async def _slow(_event):
        await gate.wait()
        return ProcessingOutcome.SUCCESS

    adapter.run_agent = _slow

    assert (await post(client, {"n": 1}, event_id="evt_a")).status == 202
    await _settle()

    deferred = await post(client, {"n": 2}, event_id="evt_b")
    assert deferred.status == 503
    # Retry-After is what turns a rejection into a deferral: Hookdeck keeps the
    # event queued and comes back rather than recording a permanent failure.
    assert int(deferred.headers["Retry-After"]) > 0

    gate.set()
    await _settle()
    assert (await post(client, {"n": 2}, event_id="evt_b", attempt=2)).status == 202


async def test_a_deferred_event_is_not_recorded_as_seen(client_factory):
    # If the ledger recorded the 503'd delivery, Hookdeck's redelivery would be
    # deduped away and the event would be lost.
    adapter, client = await client_factory({"default": {}}, max_concurrent=1)
    gate = asyncio.Event()

    async def _slow(_event):
        await gate.wait()
        return ProcessingOutcome.SUCCESS

    adapter.run_agent = _slow
    await post(client, {"n": 1}, event_id="evt_a")
    await _settle()
    await post(client, {"n": 2}, event_id="evt_b")

    assert adapter._ledger.get("evt_b") is None
    gate.set()
    await _settle()


# ----------------------------------------------------------------------
# Outcome reporting
# ----------------------------------------------------------------------


async def test_a_failed_run_is_handed_back_to_hookdeck(client_factory):
    adapter, client = await client_factory({"default": {}})

    async def _fail(_event):
        return ProcessingOutcome.FAILURE

    adapter.run_agent = _fail
    await post(client, {"n": 1}, event_id="evt_fail")
    await _settle()

    assert adapter._api.retried == ["evt_fail"]
    assert adapter._ledger.get("evt_fail")["status"] == "failed"


async def test_a_successful_run_is_not_retried(client_factory):
    adapter, client = await client_factory({"default": {}})
    adapter.run_agent = lambda event: _record([], event)

    await post(client, {"n": 1}, event_id="evt_ok")
    await _settle()

    assert adapter._api.retried == []
    assert adapter._ledger.get("evt_ok")["status"] == "succeeded"


async def test_retries_stop_at_the_configured_ceiling(client_factory):
    adapter, client = await client_factory({"default": {}}, max_agent_retries=2)

    async def _fail(_event):
        return ProcessingOutcome.FAILURE

    adapter.run_agent = _fail

    for attempt in (1, 2, 3):
        await post(client, {"n": 1}, event_id="evt_bad", attempt=attempt)
        await _settle()

    # Two redelivery requests, then the event is marked exhausted rather than
    # looping forever and burning tokens on work that keeps failing.
    assert adapter._api.retried == ["evt_bad", "evt_bad"]
    assert adapter._ledger.get("evt_bad")["status"] == "exhausted"


async def test_a_failing_retry_api_does_not_break_the_run(client_factory):
    from hookdeck.api import HookdeckAPIError

    adapter, client = await client_factory({"default": {}})
    adapter._api.fail_with = HookdeckAPIError(500, "POST", "/events/x/retry", "nope")

    async def _fail(_event):
        return ProcessingOutcome.FAILURE

    adapter.run_agent = _fail
    await post(client, {"n": 1}, event_id="evt_x")
    await _settle()

    assert adapter._ledger.get("evt_x")["status"] == "failed"


async def test_the_concurrency_slot_is_released_after_a_run(client_factory):
    adapter, client = await client_factory({"default": {}}, max_concurrent=1)
    adapter.run_agent = lambda event: _record([], event)

    await post(client, {"n": 1}, event_id="evt_1")
    await _settle()
    assert adapter._inflight == {}
    assert (await post(client, {"n": 2}, event_id="evt_2")).status == 202


# ----------------------------------------------------------------------
# Ack modes
# ----------------------------------------------------------------------


async def test_sync_mode_reports_the_agents_real_outcome(client_factory):
    adapter, client = await client_factory({"default": {}}, ack_mode="sync")
    adapter.run_agent = lambda event: _record([], event)

    response = await post(client, {"n": 1}, event_id="evt_sync")
    assert response.status == 200
    assert (await response.json())["status"] == "processed"


async def test_sync_mode_returns_5xx_when_the_run_fails(client_factory):
    adapter, client = await client_factory({"default": {}}, ack_mode="sync")

    async def _fail(_event):
        return ProcessingOutcome.FAILURE

    adapter.run_agent = _fail
    response = await post(client, {"n": 1}, event_id="evt_sync_fail")
    # 5xx puts the event back on Hookdeck's own retry schedule.
    assert response.status == 500


async def test_sync_mode_degrades_to_202_on_a_slow_run(client_factory):
    adapter, client = await client_factory(
        {"default": {}}, ack_mode="sync", sync_timeout_seconds=0.05
    )
    gate = asyncio.Event()

    async def _slow(_event):
        await gate.wait()
        return ProcessingOutcome.SUCCESS

    adapter.run_agent = _slow
    response = await post(client, {"n": 1}, event_id="evt_slow")
    # Answering 5xx here would make Hookdeck redeliver work that is still
    # running, so a long run degrades to the async contract instead.
    assert response.status == 202
    gate.set()
    await _settle()


# ----------------------------------------------------------------------
# Direct delivery
# ----------------------------------------------------------------------


async def test_deliver_only_routes_skip_the_agent(client_factory):
    adapter, client = await client_factory(
        {"default": {"deliver_only": True, "deliver": "slack", "prompt": "alert"}}
    )
    seen: list[Any] = []
    adapter.run_agent = lambda event: _record(seen, event)

    response = await post(client, {"n": 1})
    assert response.status == 200
    assert (await response.json())["status"] == "delivered"
    await _settle()
    assert seen == []
    assert adapter.direct_deliveries[0][0] == "alert"


async def test_a_rejected_direct_delivery_returns_5xx(client_factory):
    adapter, client = await client_factory(
        {"default": {"deliver_only": True, "deliver": "slack"}}
    )
    adapter.direct_deliver_result = SendResult(success=False, error="nope")

    response = await post(client, {"n": 1})
    # 5xx so Hookdeck retries rather than recording a delivered event.
    assert response.status == 502


# ----------------------------------------------------------------------
# Startup validation
# ----------------------------------------------------------------------


def test_startup_refuses_an_unknown_ack_mode(tmp_path):
    with pytest.raises(ValueError, match="ack_mode"):
        make_adapter(tmp_path, {"default": {}}, ack_mode="whenever")


def test_startup_refuses_a_missing_secret(tmp_path, monkeypatch):
    monkeypatch.delenv("HOOKDECK_WEBHOOK_SECRET", raising=False)
    config = PlatformConfig(
        extra={"mode": "push", "host": "0.0.0.0", "secret": "", "routes": {"a": {}}}
    )
    with pytest.raises(ValueError, match="signing secret"):
        HookdeckAdapter(config)._validate_startup()


def test_startup_refuses_insecure_no_auth_off_loopback(tmp_path):
    config = PlatformConfig(
        extra={
            "mode": "push",
            "host": "0.0.0.0",
            "secret": "INSECURE_NO_AUTH",
            "routes": {"a": {}},
        }
    )
    with pytest.raises(ValueError, match="non-loopback"):
        HookdeckAdapter(config)._validate_startup()


def test_startup_refuses_deliver_only_without_a_target(tmp_path):
    with pytest.raises(ValueError, match="deliver_only"):
        make_adapter(tmp_path, {"a": {"deliver_only": True}})


def test_cli_mode_needs_a_source_to_listen_to(tmp_path):
    config = PlatformConfig(
        extra={"mode": "cli", "secret": SECRET, "routes": {"a": {}}}
    )
    with pytest.raises(ValueError, match="needs a Hookdeck source"):
        HookdeckAdapter(config)._validate_startup()


def test_each_route_gets_its_own_tunnel():
    # `hookdeck listen` forwards a single source, so a multi-source gateway
    # needs one process per route rather than one overall.
    config = PlatformConfig(
        extra={
            "mode": "cli",
            "secret": SECRET,
            "routes": {"github-prs": {"source": "github"}, "stripe": {"source": "stripe"}},
        }
    )
    adapter = HookdeckAdapter(config)
    adapter._validate_startup()
    assert adapter._tunnel_plan() == {"github-prs": "github", "stripe": "stripe"}


def test_a_platform_level_source_covers_routes_that_omit_one():
    config = PlatformConfig(
        extra={
            "mode": "cli",
            "secret": SECRET,
            "source": "shopify",
            "routes": {"orders": {}},
        }
    )
    assert HookdeckAdapter(config)._tunnel_plan() == {"orders": "shopify"}


def test_cli_mode_forces_a_loopback_bind(tmp_path):
    config = PlatformConfig(
        extra={"mode": "cli", "host": "0.0.0.0", "secret": SECRET, "routes": {"a": {}}}
    )
    adapter = HookdeckAdapter(config)
    assert adapter._host == "127.0.0.1"


# ----------------------------------------------------------------------
# Unparseable payloads and last-attempt detection
# ----------------------------------------------------------------------


async def test_an_unparseable_body_is_rejected_without_cancelling_retries(client_factory):
    adapter, client = await client_factory({"default": {}})
    raw = b"\xff\xfe not json, not form"
    headers = {
        "content-type": "application/json",
        "x-hookdeck-eventid": "evt_bad_body",
        "x-hookdeck-signature": compute_signature(raw, SECRET),
    }
    response = await client.post("/hookdeck", data=raw, headers=headers)
    assert response.status == 400
    # Default is off: cancelling retries on a parser's say-so is unrecoverable
    # once retention lapses, so it must be opted into.
    assert "Retry-After" not in response.headers
    assert adapter._ledger.get("evt_bad_body") is None


async def test_cancel_on_unparseable_emits_retry_after_minus_one(client_factory):
    adapter, client = await client_factory(
        {"default": {}}, cancel_retries_on_unparseable=True
    )
    raw = b"\xff\xfe not json, not form"
    headers = {
        "content-type": "application/json",
        "x-hookdeck-eventid": "evt_bad_body",
        "x-hookdeck-signature": compute_signature(raw, SECRET),
    }
    response = await client.post("/hookdeck", data=raw, headers=headers)
    assert response.status == 400
    # Hookdeck reads -1 as "cancel all further automatic retries".
    assert response.headers["Retry-After"] == "-1"
    # Recorded so `status` can surface how often this fires.
    assert adapter._ledger.get("evt_bad_body")["status"] == "cancelled"


async def test_a_present_will_retry_after_header_means_more_attempts_are_coming(
    client_factory,
):
    adapter, client = await client_factory({"default": {}})
    seen: list[Any] = []
    adapter.run_agent = lambda event: _record(seen, event)

    await post(
        client,
        {"n": 1},
        event_id="evt_more",
        extra_headers={"x-hookdeck-will-retry-after": "2026-08-10T12:00:00Z"},
    )
    await _settle()
    assert seen[0].metadata["hookdeck_last_automatic_attempt"] is False


async def test_an_absent_will_retry_after_header_flags_the_last_attempt(client_factory):
    adapter, client = await client_factory({"default": {}})
    seen: list[Any] = []
    adapter.run_agent = lambda event: _record(seen, event)

    await post(client, {"n": 1}, event_id="evt_last")
    await _settle()
    assert seen[0].metadata["hookdeck_last_automatic_attempt"] is True


async def test_a_failure_on_the_last_attempt_still_requests_redelivery(client_factory):
    # Automatic retries are exhausted, but manual retries are unlimited — so
    # the request is still worth making, just noisily.
    adapter, client = await client_factory({"default": {}})

    async def _fail(_event):
        return ProcessingOutcome.FAILURE

    adapter.run_agent = _fail
    await post(client, {"n": 1}, event_id="evt_last_fail")
    await _settle()
    assert adapter._api.retried == ["evt_last_fail"]


# ----------------------------------------------------------------------
# Boot-time recovery of interrupted runs
# ----------------------------------------------------------------------


async def test_runs_interrupted_by_a_shutdown_are_recovered_on_boot(client_factory):
    # The adapter acked 202 the moment it admitted the event, so Hookdeck has
    # recorded that delivery as successful and will never bring it back. Only
    # an explicit retry recovers it — which is why the ledger never prunes
    # `running` rows.
    adapter, _client = await client_factory({"default": {}})
    adapter._ledger.admit("evt_orphan", route="default", attempt=1)

    assert await adapter._recover_orphaned_runs() == 1
    assert adapter._api.retried == ["evt_orphan"]
    assert adapter._ledger.get("evt_orphan")["status"] == "failed"


async def test_recovery_skips_events_that_already_spent_their_budget(client_factory):
    adapter, _client = await client_factory({"default": {}}, max_agent_retries=1)
    adapter._ledger.admit("evt_spent", route="default", attempt=1)
    adapter._ledger.admit("evt_spent", route="default", attempt=2)  # second run

    assert await adapter._recover_orphaned_runs() == 0
    assert adapter._api.retried == []
    assert adapter._ledger.get("evt_spent")["status"] == "exhausted"


async def test_recovery_leaves_completed_deliveries_alone(client_factory):
    adapter, _client = await client_factory({"default": {}})
    adapter._ledger.admit("evt_done", route="default", attempt=1)
    adapter._ledger.mark_succeeded("evt_done")

    assert await adapter._recover_orphaned_runs() == 0
    assert adapter._api.retried == []


async def test_recovery_can_be_turned_off(client_factory):
    adapter, _client = await client_factory({"default": {}}, recover_on_boot=False)
    adapter._ledger.admit("evt_orphan", route="default", attempt=1)

    assert await adapter._recover_orphaned_runs() == 0
    assert adapter._api.retried == []
    # Left running, so `doctor` still reports it for a human to decide on.
    assert adapter._ledger.get("evt_orphan")["status"] == "running"


async def test_a_failing_recovery_call_does_not_block_startup(client_factory):
    from hookdeck.api import HookdeckAPIError

    adapter, _client = await client_factory({"default": {}})
    adapter._ledger.admit("evt_orphan", route="default", attempt=1)
    adapter._api.fail_with = HookdeckAPIError(500, "POST", "/events/x/retry", "nope")

    assert await adapter._recover_orphaned_runs() == 0


async def test_cli_mode_binds_both_loopback_families(client_factory):
    # The CLI forwards to http://localhost:<port>, which resolves to ::1 first
    # on a dual-stack machine. An IPv4-only listener refuses every delivery
    # while the tunnel itself looks healthy.
    adapter, _client = await client_factory({"a": {"source": "s"}}, mode="cli")
    assert adapter._bind_hosts() == ["127.0.0.1", "::1"]


async def test_push_mode_binds_only_the_configured_host(client_factory):
    adapter, _client = await client_factory({"a": {}}, mode="push", host="127.0.0.1")
    assert adapter._bind_hosts() == ["127.0.0.1"]
