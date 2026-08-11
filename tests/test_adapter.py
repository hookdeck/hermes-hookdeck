"""End-to-end tests of the ingest path against the stubbed Hermes internals."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from hookdeck.adapter import HookdeckAdapter
from hookdeck.constants import WEBHOOK_SECRET_ENV
from hookdeck.ledger import RunLedger
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
    adapter._ledger = RunLedger(tmp_path / "state.db")
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


async def test_a_malformed_signature_is_refused_not_a_server_error(client_factory):
    """A junk signature must answer 401, like any other forged one.

    A non-ASCII byte in the header used to raise out of the verification path
    and become a 500 — which skips ``assert_declared_status`` entirely and,
    because the provisioned retry rule covers ``500-599``, had Hookdeck retry
    an unauthenticated request instead of dropping it.
    """
    _adapter, client = await client_factory({"default": {}})
    response = await post(
        client,
        {"hello": "world"},
        sign=False,
        extra_headers={"x-hookdeck-signature": "not-base64-é"},
    )
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
    monkeypatch.delenv(WEBHOOK_SECRET_ENV, raising=False)
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
    # Still recorded, as `would_cancel`. "Measure how often this would fire
    # before enabling it" needs something to measure, and a log line is not it.
    assert adapter._ledger.get("evt_bad_body")["status"] == "would_cancel"


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


# ----------------------------------------------------------------------
# Encoding
# ----------------------------------------------------------------------


async def _post_raw(client, raw: bytes, event_id: str):
    return await client.post(
        "/hookdeck",
        data=raw,
        headers={
            "content-type": "application/json",
            "x-hookdeck-eventid": event_id,
            "x-hookdeck-signature": compute_signature(raw, SECRET),
        },
    )


async def test_a_lone_surrogate_body_is_rejected_at_the_door(client_factory):
    # json.loads decodes with `surrogatepass`, so CESU-8 parses cleanly and
    # renders into the prompt cleanly, then raises UnicodeEncodeError at the
    # network boundary inside the agent run — after the ack, in a layer with no
    # idea why. Reject it here instead.
    adapter, client = await client_factory({"default": {}})
    seen: list[Any] = []
    adapter.run_agent = lambda event: _record(seen, event)

    response = await _post_raw(client, b'{"a": "\xed\xa0\x80"}', "evt_surrogate")
    assert response.status == 400
    await _settle()
    assert seen == []


async def test_a_utf16_body_is_rejected(client_factory):
    # RFC 8259 §8.1 requires UTF-8 for JSON exchanged between systems, but
    # json.loads sniffs the BOM and accepts UTF-16 happily.
    adapter, client = await client_factory({"default": {}})
    seen: list[Any] = []
    adapter.run_agent = lambda event: _record(seen, event)

    response = await _post_raw(client, '{"a": "ok"}'.encode("utf-16"), "evt_utf16")
    assert response.status == 400
    await _settle()
    assert seen == []


async def test_valid_utf8_multibyte_content_still_works(client_factory):
    adapter, client = await client_factory({"default": {}})
    seen: list[Any] = []
    adapter.run_agent = lambda event: _record(seen, event)

    response = await _post_raw(client, '{"a": "café 日本 🪝"}'.encode(), "evt_utf8")
    assert response.status == 202
    await _settle()
    assert seen[0].raw_message["a"] == "café 日本 🪝"


async def test_form_encoded_bodies_still_work(client_factory):
    adapter, client = await client_factory({"default": {}})
    seen: list[Any] = []
    adapter.run_agent = lambda event: _record(seen, event)

    response = await _post_raw(client, b"kind=ping&who=bob", "evt_form")
    assert response.status == 202
    await _settle()
    assert seen[0].raw_message == {"kind": "ping", "who": "bob"}


async def test_an_escaped_lone_surrogate_is_replaced_not_rejected(client_factory):
    # RFC 8259 permits any \uXXXX escape including an unpaired surrogate, and
    # the body is pure ASCII on the wire — so it is valid JSON that the strict
    # decode cannot catch. Python then produces a str that raises
    # UnicodeEncodeError inside the agent run, after the ack.
    adapter, client = await client_factory({"default": {}})
    seen: list[Any] = []
    adapter.run_agent = lambda event: _record(seen, event)

    response = await _post_raw(client, b'{"a": "hi \\ud800 there"}', "evt_escaped")
    assert response.status == 202
    await _settle()
    assert seen[0].raw_message["a"] == "hi � there"
    # Substituting silently is the failure mode this area is about, so it is
    # recorded rather than merely done.
    assert seen[0].metadata["hookdeck_surrogates_replaced"] is True
    # And the result is actually encodable, which was the whole point.
    seen[0].raw_message["a"].encode("utf-8")


async def test_a_valid_surrogate_pair_is_left_alone(client_factory):
    adapter, client = await client_factory({"default": {}})
    seen: list[Any] = []
    adapter.run_agent = lambda event: _record(seen, event)

    response = await _post_raw(client, b'{"a": "\\ud83e\\udd1d"}', "evt_pair")
    assert response.status == 202
    await _settle()
    assert seen[0].raw_message["a"] == "\U0001f91d"
    assert seen[0].metadata["hookdeck_surrogates_replaced"] is False


async def test_nested_and_listed_surrogates_are_reached(client_factory):
    adapter, client = await client_factory({"default": {}})
    seen: list[Any] = []
    adapter.run_agent = lambda event: _record(seen, event)

    await _post_raw(client, b'{"a": {"b": ["ok", "x\\ud800"]}}', "evt_nested")
    await _settle()
    assert seen[0].raw_message["a"]["b"] == ["ok", "x�"]


# ----------------------------------------------------------------------
# Gateway authorization
# ----------------------------------------------------------------------


async def test_verified_routes_declare_upstream_authorization(client_factory):
    # Without this the gateway refuses every delivery as
    # "Unauthorized user: hookdeck:<route>". Core exempts the built-in webhook
    # platform by enum member, on the grounds that HMAC verification in the
    # adapter is the authorization — the same reasoning applies here, but the
    # membership test cannot, since this is Platform.HOOKDECK.
    adapter, _client = await client_factory({"default": {}})
    assert adapter.authorization_is_upstream is True


def test_unverified_routes_do_not_claim_upstream_authorization(tmp_path):
    # INSECURE_NO_AUTH skips signature checking, so there is no upstream
    # decision to delegate to and the local allowlist must still apply. This is
    # narrower than core's blanket exemption, which covers built-in webhook
    # routes even when they skip verification.
    config = PlatformConfig(
        extra={
            "mode": "push",
            "host": "127.0.0.1",
            "secret": "INSECURE_NO_AUTH",
            "routes": {"a": {}},
        }
    )
    assert HookdeckAdapter(config).authorization_is_upstream is False


# ----------------------------------------------------------------------
# Forwarded paths and deferral budget
# ----------------------------------------------------------------------


async def test_a_forwarded_provider_path_still_matches_its_route(client_factory):
    # Hookdeck appends the source request's path unless path_forwarding_disabled
    # is set, so a provider POSTing to <source-url>/events arrives here as
    # /hookdeck/<route>/events. Matching a single segment would 404 it.
    adapter, client = await client_factory({"stripe": {"source": "s"}})
    seen: list[Any] = []
    adapter.run_agent = lambda event: _record(seen, event)

    response = await post(client, {"n": 1}, path="/hookdeck/stripe/events/v2")
    assert response.status == 202
    await _settle()
    assert seen[0].metadata["hookdeck_route"] == "stripe"


async def test_a_similar_route_name_is_not_swallowed(client_factory):
    # Segment matching, not string prefix: `stripe` must not claim traffic for
    # `stripe-test`.
    adapter, client = await client_factory(
        {"stripe": {"source": "a"}, "stripe-test": {"source": "b"}}
    )
    seen: list[Any] = []
    adapter.run_agent = lambda event: _record(seen, event)

    await post(client, {"n": 1}, path="/hookdeck/stripe-test/events")
    await _settle()
    assert seen[0].metadata["hookdeck_route"] == "stripe-test"


async def test_retry_after_is_dropped_once_saturation_stops_being_transient(
    client_factory,
):
    # Retry-After overrides the connection's retry rule, so a fixed short
    # interval on a persistent condition spends the whole automatic budget in
    # minutes. It is sent only while "capacity frees in seconds" still holds.
    adapter, client = await client_factory(
        {"default": {}}, max_concurrent=1, defer_attempt_limit=2
    )
    gate = asyncio.Event()

    async def _slow(_event):
        await gate.wait()
        return ProcessingOutcome.SUCCESS

    adapter.run_agent = _slow
    await post(client, {"n": 1}, event_id="evt_hold")
    await _settle()

    early = await post(client, {"n": 2}, event_id="evt_b", attempt=1)
    assert early.status == 503 and "Retry-After" in early.headers

    late = await post(client, {"n": 2}, event_id="evt_b", attempt=9)
    assert late.status == 503
    assert "Retry-After" not in late.headers

    gate.set()
    await _settle()


async def test_a_delivery_with_no_event_id_is_processed_but_warned_about(
    client_factory, caplog
):
    adapter, client = await client_factory({"default": {}})
    seen: list[Any] = []
    adapter.run_agent = lambda event: _record(seen, event)

    raw = json.dumps({"n": 1}).encode()
    response = await client.post(
        "/hookdeck",
        data=raw,
        headers={
            "content-type": "application/json",
            "x-hookdeck-signature": compute_signature(raw, SECRET),
        },
    )
    assert response.status == 202
    await _settle()
    assert len(seen) == 1
    # No id means no dedup and no retry — a real loss of guarantees, so it must
    # not degrade quietly.
    assert "without deduplication or retry" in caplog.text


async def test_a_proxys_request_id_is_not_mistaken_for_an_event_id(
    client_factory, caplog
):
    """`X-Request-ID` is not a Hookdeck identifier and must not stand in.

    Anything in front of the gateway can set it, it is not subject to
    `header_prefix`, and one Hookdeck request fans out to one event per
    matching connection — so it is not even unique per delivery. Accepting it
    would key the ledger on the wrong thing and silence the warning that tells
    an operator their `header_prefix` is wrong.
    """
    adapter, client = await client_factory({"default": {}})
    seen: list[Any] = []
    adapter.run_agent = lambda event: _record(seen, event)

    raw = json.dumps({"n": 1}).encode()
    response = await client.post(
        "/hookdeck",
        data=raw,
        headers={
            "content-type": "application/json",
            "x-hookdeck-signature": compute_signature(raw, SECRET),
            "X-Request-ID": "req_from_some_proxy",
        },
    )
    assert response.status == 202
    await _settle()
    assert "without deduplication or retry" in caplog.text
    assert adapter._ledger is not None
    assert adapter._ledger.get("req_from_some_proxy") is None


# ----------------------------------------------------------------------
# Deduplication precedence
# ----------------------------------------------------------------------


async def test_a_duplicate_at_capacity_is_answered_as_a_duplicate(client_factory):
    # If a duplicate were deferred instead, Hookdeck would redeliver it with a
    # higher attempt number — which reads as new work, and runs the agent a
    # second time for an event already handled.
    adapter, client = await client_factory({"default": {}}, max_concurrent=1)
    seen: list[Any] = []
    adapter.run_agent = lambda event: _record(seen, event)

    await post(client, {"n": 1}, event_id="evt_a", attempt=1)
    await _settle()

    gate = asyncio.Event()

    async def _slow(_event):
        await gate.wait()
        return ProcessingOutcome.SUCCESS

    adapter.run_agent = _slow
    await post(client, {"n": 2}, event_id="evt_b", attempt=1)  # occupies the slot
    await _settle()

    repeat = await post(client, {"n": 1}, event_id="evt_a", attempt=1)
    assert repeat.status == 200
    assert (await repeat.json())["status"] == "duplicate"

    gate.set()
    await _settle()
    assert len(seen) == 1


async def test_deliver_only_routes_are_deduplicated_too(client_factory):
    # A repeat here posts to a channel a human is reading. Hookdeck's signature
    # carries no timestamp, so this ledger check is the only replay protection
    # these routes get.
    adapter, client = await client_factory(
        {"default": {"deliver_only": True, "deliver": "slack", "prompt": "alert"}}
    )

    first = await post(client, {"n": 1}, event_id="evt_dup", attempt=1)
    assert (await first.json())["status"] == "delivered"

    second = await post(client, {"n": 1}, event_id="evt_dup", attempt=1)
    assert (await second.json())["status"] == "duplicate"
    assert len(adapter.direct_deliveries) == 1


async def test_a_redelivery_does_not_reuse_the_previous_runs_identity(client_factory):
    adapter, client = await client_factory({"default": {}})
    seen: list[Any] = []
    adapter.run_agent = lambda event: _record(seen, event)

    await post(client, {"n": 1}, event_id="evt_x", attempt=1)
    await _settle()
    await post(client, {"n": 1}, event_id="evt_x", attempt=2)
    await _settle()

    # Distinct session ids: otherwise a redelivery arriving while the previous
    # run is still going overwrites its in-flight entry, and the two runs
    # record each other's outcomes.
    assert seen[0].source.chat_id != seen[1].source.chat_id


async def test_an_exhausted_event_is_not_admitted_again(client_factory):
    # Hookdeck's own retries keep arriving in sync mode; without this the
    # retry budget would be spent by them rather than by max_agent_retries.
    adapter, client = await client_factory({"default": {}}, max_agent_retries=1)

    async def _fail(_event):
        return ProcessingOutcome.FAILURE

    adapter.run_agent = _fail
    for attempt in (1, 2):
        await post(client, {"n": 1}, event_id="evt_spent", attempt=attempt)
        await _settle()
    assert adapter._ledger.get("evt_spent")["status"] == "exhausted"

    later = await post(client, {"n": 1}, event_id="evt_spent", attempt=3)
    assert (await later.json())["status"] == "duplicate"


# ----------------------------------------------------------------------
# Handing failures back
# ----------------------------------------------------------------------


async def test_a_transient_api_failure_does_not_strand_the_event(client_factory, monkeypatch):
    from hookdeck.api import HookdeckAPIError

    monkeypatch.setattr("hookdeck.adapter._REDELIVERY_RETRY_INITIAL_SECONDS", 0.0)
    adapter, client = await client_factory({"default": {}})

    calls = {"n": 0}

    async def _flaky(event_id: str):
        calls["n"] += 1
        if calls["n"] == 1:
            raise HookdeckAPIError(503, "POST", "/retry", "temporarily unavailable")
        adapter._api.retried.append(event_id)

    adapter._api.retry_event = _flaky

    async def _fail(_event):
        return ProcessingOutcome.FAILURE

    adapter.run_agent = _fail
    await post(client, {"n": 1}, event_id="evt_flaky")
    await _settle()
    await asyncio.sleep(0.05)

    # The 202 is already sent, so a single un-retried call losing to a network
    # blip would strand the event with nothing left to bring it back.
    assert adapter._api.retried == ["evt_flaky"]


async def test_a_timed_out_sync_run_still_hands_its_failure_back(client_factory, monkeypatch):
    # Once sync degrades to 202, Hookdeck records the delivery as successful —
    # so its own retry rules will not fire, and this is the async situation.
    monkeypatch.setattr("hookdeck.adapter._REDELIVERY_RETRY_INITIAL_SECONDS", 0.0)
    adapter, client = await client_factory(
        {"default": {}}, ack_mode="sync", sync_timeout_seconds=0.05
    )
    gate = asyncio.Event()

    async def _slow_failure(_event):
        await gate.wait()
        return ProcessingOutcome.FAILURE

    adapter.run_agent = _slow_failure
    response = await post(client, {"n": 1}, event_id="evt_slow_fail")
    assert response.status == 202

    gate.set()
    await _settle()
    assert adapter._api.retried == ["evt_slow_fail"]


async def test_an_abandoned_hand_back_still_clears_its_sync_marker(
    client_factory, monkeypatch
):
    """The marker goes once the hand-back decision is made, not once it works.

    Keeping it on the failure path grows the set for the life of the process,
    and a later sync-mode run of the same id would be treated as having been
    acked early when it was not.
    """
    from hookdeck.api import HookdeckAPIError

    monkeypatch.setattr("hookdeck.adapter._REDELIVERY_RETRY_INITIAL_SECONDS", 0.0)
    adapter, client = await client_factory(
        {"default": {}}, ack_mode="sync", sync_timeout_seconds=0.05
    )
    adapter._api.fail_with = HookdeckAPIError(0, "POST", "/events/x/retry", "no route")
    gate = asyncio.Event()

    async def _slow_failure(_event):
        await gate.wait()
        return ProcessingOutcome.FAILURE

    adapter.run_agent = _slow_failure
    assert (await post(client, {"n": 1}, event_id="evt_abandoned")).status == 202
    assert "evt_abandoned" in adapter._acked_before_completion

    gate.set()
    await _settle()
    assert adapter._api.retried == []
    assert adapter._acked_before_completion == set()


async def test_an_exhausted_event_clears_its_sync_marker_too(client_factory):
    adapter, client = await client_factory(
        {"default": {}},
        ack_mode="sync",
        sync_timeout_seconds=0.05,
        max_agent_retries=0,
    )
    gate = asyncio.Event()

    async def _slow_failure(_event):
        await gate.wait()
        return ProcessingOutcome.FAILURE

    adapter.run_agent = _slow_failure
    assert (await post(client, {"n": 1}, event_id="evt_spent")).status == 202

    gate.set()
    await _settle()
    assert adapter._acked_before_completion == set()


async def test_a_sync_failure_within_the_timeout_is_left_to_hookdeck(client_factory):
    # The 5xx response *is* the retry request there; asking again would double
    # the redeliveries.
    adapter, client = await client_factory({"default": {}}, ack_mode="sync")

    async def _fail(_event):
        return ProcessingOutcome.FAILURE

    adapter.run_agent = _fail
    assert (await post(client, {"n": 1}, event_id="evt_sync_fail")).status == 500
    await _settle()
    assert adapter._api.retried == []


async def test_an_operator_replay_of_an_exhausted_event_runs_again(client_factory):
    # The dashboard's Replay button, `hermes hookdeck replay` and the agent's
    # retry tool all arrive as a MANUAL redelivery. Answering 200 to those
    # would make every recovery path this code recommends a silent no-op.
    adapter, client = await client_factory({"default": {}}, max_agent_retries=1)

    async def _fail(_event):
        return ProcessingOutcome.FAILURE

    adapter.run_agent = _fail
    for attempt in (1, 2):
        await post(client, {"n": 1}, event_id="evt_dead", attempt=attempt)
        await _settle()
    assert adapter._ledger.get("evt_dead")["status"] == "exhausted"

    seen: list[Any] = []
    adapter.run_agent = lambda event: _record(seen, event)
    replay = await post(
        client,
        {"n": 1},
        event_id="evt_dead",
        attempt=3,
        extra_headers={"x-hookdeck-attempt-trigger": "MANUAL"},
    )
    assert replay.status == 202
    await _settle()
    assert len(seen) == 1
    assert adapter._ledger.get("evt_dead")["status"] == "succeeded"


async def test_hookdecks_own_retry_of_an_exhausted_event_is_still_refused(client_factory):
    adapter, client = await client_factory({"default": {}}, max_agent_retries=1)

    async def _fail(_event):
        return ProcessingOutcome.FAILURE

    adapter.run_agent = _fail
    for attempt in (1, 2):
        await post(client, {"n": 1}, event_id="evt_dead", attempt=attempt)
        await _settle()

    automatic = await post(
        client,
        {"n": 1},
        event_id="evt_dead",
        attempt=3,
        extra_headers={"x-hookdeck-attempt-trigger": "AUTOMATIC"},
    )
    body = await automatic.json()
    assert automatic.status == 200
    # The body says why, so Hookdeck's event log is not left claiming the
    # delivery was a duplicate when the budget is what ran out.
    assert "budget" in body["reason"]


async def test_a_successful_run_clears_its_timed_out_sync_marker(client_factory):
    # Otherwise the id leaks for the life of the gateway and subtly changes how
    # a later failure of the same id is handled.
    adapter, client = await client_factory(
        {"default": {}}, ack_mode="sync", sync_timeout_seconds=0.05
    )
    gate = asyncio.Event()

    async def _slow_success(_event):
        await gate.wait()
        return ProcessingOutcome.SUCCESS

    adapter.run_agent = _slow_success
    assert (await post(client, {"n": 1}, event_id="evt_slow_ok")).status == 202
    assert "evt_slow_ok" in adapter._acked_before_completion

    gate.set()
    await _settle()
    assert "evt_slow_ok" not in adapter._acked_before_completion


async def test_a_due_pause_is_resumed_at_boot(client_factory):
    # The agent's pause tool records a deadline rather than holding a timer,
    # because a restart is one of the main reasons to pause.
    import time as _time

    adapter, _client = await client_factory({"default": {}})
    resumed: list[str] = []

    async def _unpause(connection_id: str):
        resumed.append(connection_id)

    adapter._api.unpause_connection = _unpause
    adapter._ledger.schedule_resume("web_due", "mine", _time.time() - 1)
    adapter._ledger.schedule_resume("web_later", "mine", _time.time() + 3600)

    assert await adapter._resume_due_connections() == 1
    assert resumed == ["web_due"]
    # Cleared, so it does not fire again against a later pause.
    assert [r["connection_id"] for r in adapter._ledger.due_resumes()] == []


async def test_upkeep_runs_without_any_traffic(client_factory, monkeypatch):
    # A paused connection produces no deliveries, so a resume deadline driven
    # off the request path would never come due — the tool promises a pause
    # that ends, and on a quiet gateway it would not.
    import time as _time

    monkeypatch.setattr("hookdeck.adapter._MAINTENANCE_INTERVAL_SECONDS", 0.01)
    adapter, _client = await client_factory({"default": {}})
    resumed: list[str] = []

    async def _unpause(connection_id: str):
        resumed.append(connection_id)

    adapter._api.unpause_connection = _unpause
    adapter._ledger.schedule_resume("web_due", "mine", _time.time() - 1)

    adapter._maintenance = asyncio.create_task(adapter._maintain())
    try:
        for _ in range(50):
            await asyncio.sleep(0.01)
            if resumed:
                break
    finally:
        await adapter._stop_maintenance()

    assert resumed == ["web_due"]


async def test_a_failing_upkeep_tick_does_not_stop_the_adapter(client_factory, monkeypatch):
    monkeypatch.setattr("hookdeck.adapter._MAINTENANCE_INTERVAL_SECONDS", 0.01)
    adapter, _client = await client_factory({"default": {}})

    calls = {"n": 0}

    async def _explode() -> int:
        calls["n"] += 1
        raise RuntimeError("boom")

    adapter._resume_due_connections = _explode
    adapter._maintenance = asyncio.create_task(adapter._maintain())
    try:
        for _ in range(50):
            await asyncio.sleep(0.01)
            if calls["n"] >= 2:
                break
    finally:
        await adapter._stop_maintenance()

    # Still ticking after the first failure: upkeep must not be the thing that
    # takes the adapter down.
    assert calls["n"] >= 2
