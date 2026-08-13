"""Tests for the agent-callable tools.

These are the only surface an LLM drives unsupervised against a live Hookdeck
project, and four of the seven mutate it. So the emphasis here is less on
return-value shape than on the things that decide *what gets mutated*: the
pause ceiling, which connection a name resolves to, and whether a scheduled
resume is written where the adapter will actually find it.
"""

from __future__ import annotations

import json
import time
from typing import ClassVar

import pytest

from hookdeck import tools
from hookdeck.api import HookdeckAPIError


class FakeAPI:
    """Stands in for :class:`HookdeckAPI`, recording what was asked of it.

    ``calls`` is the assertion surface: these tools are interesting for the
    requests they make, not the strings they return.
    """

    #: Set per-test. Every instance the handler constructs shares it, because
    #: the handler constructs its own and there is no injection point.
    responses: ClassVar[dict] = {}
    calls: ClassVar[list] = []
    raises: ClassVar[Exception | None] = None
    #: Raise only for a named method, so a test can fail one call in a
    #: sequence rather than all of them.
    raises_for: ClassVar[dict] = {}

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc) -> None:
        return None

    async def _record(self, _name: str, /, *args, **kwargs):
        # Positional-only: callers pass through their own kwargs, and
        # `list_connections(name=…)` would otherwise collide with this one.
        type(self).calls.append((_name, args, kwargs))
        per_method = type(self).raises_for.get(_name)
        if per_method is not None:
            raise per_method
        if type(self).raises is not None:
            raise type(self).raises
        return type(self).responses.get(_name, {})

    async def queue_depth(self, **kw):
        return await self._record("queue_depth", **kw)

    async def list_events(self, **kw):
        return await self._record("list_events", **kw)

    async def list_issues(self, **kw):
        return await self._record("list_issues", **kw)

    async def count_issues(self, **kw):
        return await self._record("count_issues", **kw) or 0

    async def get_event_raw_body(self, event_id):
        return await self._record("get_event_raw_body", event_id)

    async def retry_event(self, event_id):
        return await self._record("retry_event", event_id)

    async def bulk_retry_events(self, query):
        return await self._record("bulk_retry_events", query)

    async def list_connections(self, **kw):
        return await self._record("list_connections", **kw)

    async def pause_connection(self, connection_id):
        return await self._record("pause_connection", connection_id)

    async def unpause_connection(self, connection_id):
        return await self._record("unpause_connection", connection_id)


@pytest.fixture()
def api(monkeypatch):
    FakeAPI.responses, FakeAPI.calls = {}, []
    FakeAPI.raises, FakeAPI.raises_for = None, {}
    monkeypatch.setattr(tools, "HookdeckAPI", FakeAPI)
    yield FakeAPI
    FakeAPI.responses, FakeAPI.calls = {}, []
    FakeAPI.raises, FakeAPI.raises_for = None, {}


@pytest.fixture()
def ledger_at(tmp_path, monkeypatch):
    """Point every ledger reader and writer at one temporary database."""
    path = tmp_path / "state.db"
    monkeypatch.setattr(
        "hookdeck.settings.load_hermes_config",
        lambda: {
            "gateway": {
                "platforms": {"hookdeck": {"extra": {"state_path": str(path)}}}
            }
        },
    )
    return path


def call(name: str, args: dict | None = None) -> str:
    """Invoke a tool the way the Hermes tool dispatcher does."""
    return tools.HANDLERS[name](args or {})


def calls_named(api, name: str) -> list:
    return [c for c in api.calls if c[0] == name]


# ----------------------------------------------------------------------
# The pause ceiling
# ----------------------------------------------------------------------
#
# MAX_PAUSE_MINUTES is the only thing between a confused agent and a
# connection paused for as long as it feels like. Every way of getting a
# number wrong has to land back inside the bounds.


@pytest.mark.parametrize(
    "given, expected",
    [
        ({}, tools.DEFAULT_PAUSE_MINUTES),
        ({"minutes": None}, tools.DEFAULT_PAUSE_MINUTES),
        ({"minutes": ""}, tools.DEFAULT_PAUSE_MINUTES),
        ({"minutes": "not a number"}, tools.DEFAULT_PAUSE_MINUTES),
        ({"minutes": []}, tools.DEFAULT_PAUSE_MINUTES),
        ({"minutes": 0}, tools.DEFAULT_PAUSE_MINUTES),  # falsy -> default
        ({"minutes": -30}, 1),
        ({"minutes": 10}, 10),
        ({"minutes": "10"}, 10),  # models emit numbers as strings
        ({"minutes": tools.MAX_PAUSE_MINUTES}, tools.MAX_PAUSE_MINUTES),
        ({"minutes": 10_000}, tools.MAX_PAUSE_MINUTES),
    ],
)
def test_a_pause_is_always_between_one_minute_and_the_cap(given, expected):
    assert tools._pause_minutes(given) == expected


def test_the_pause_the_tool_reports_is_the_one_it_scheduled(api, ledger_at):
    # The message tells the model when the connection comes back. If the
    # clamped value and the reported value could differ, the model would plan
    # against a deadline that does not exist.
    message = call("hookdeck_pause_connection", {"connection": "con_1", "minutes": 999})
    assert f"in {tools.MAX_PAUSE_MINUTES} minutes" in message

    from hookdeck.ledger import RunLedger

    ledger = RunLedger(ledger_at)
    try:
        rows = ledger.due_resumes(now=time.time() + tools.MAX_PAUSE_MINUTES * 60 + 1)
        assert [r["connection_id"] for r in rows] == ["con_1"]
    finally:
        ledger.close()


# ----------------------------------------------------------------------
# Pause and resume, end to end
# ----------------------------------------------------------------------


async def test_a_scheduled_pause_is_resumed_by_the_adapter(api, ledger_at, tmp_path):
    """The tool writes the deadline; the adapter honours it. Both halves.

    Covered separately, each half passes while disagreeing about the path or
    the row shape — and a deadline written where the adapter never looks is
    indistinguishable from the auto-resume feature silently not existing.
    """
    from hookdeck.ledger import RunLedger
    from tests.test_adapter import make_adapter

    call("hookdeck_pause_connection", {"connection": "con_1", "minutes": 5})
    assert calls_named(api, "pause_connection") == [("pause_connection", ("con_1",), {})]

    adapter = make_adapter(tmp_path, {"default": {}})
    adapter._ledger = RunLedger(ledger_at)
    adapter._api = api()
    try:
        # Nothing is due yet: a pause that resumes immediately is not a pause.
        assert await adapter._resume_due_connections() == 0

        # Reach the deadline by moving it into the past, rather than sleeping.
        adapter._ledger.schedule_resume("con_1", "con_1", time.time() - 1)
        assert await adapter._resume_due_connections() == 1
        assert calls_named(api, "unpause_connection") == [
            ("unpause_connection", ("con_1",), {})
        ]

        # And the deadline is consumed, so it cannot fire twice.
        assert await adapter._resume_due_connections() == 0
    finally:
        adapter._ledger.close()


def test_a_manual_resume_cancels_the_scheduled_one(api, ledger_at):
    # Otherwise a deadline left by an earlier short pause unpauses a later,
    # longer pause before its time — silently, and at the worst moment.
    call("hookdeck_pause_connection", {"connection": "con_1", "minutes": 1})
    call("hookdeck_resume_connection", {"connection": "con_1"})

    from hookdeck.ledger import RunLedger

    ledger = RunLedger(ledger_at)
    try:
        assert ledger.due_resumes(now=time.time() + 86_400) == []
    finally:
        ledger.close()


def test_a_ledger_that_cannot_be_opened_does_not_lose_the_pause(
    api, ledger_at, monkeypatch, caplog
):
    # The pause itself is the safety-critical half. If recording the deadline
    # fails — a read-only volume, a corrupt file — the connection must still be
    # paused. An un-expiring pause is recoverable; a dropped one is not.
    def refuse(_path):
        raise OSError("unable to open database file")

    monkeypatch.setattr("hookdeck.ledger.RunLedger", refuse)

    message = call("hookdeck_pause_connection", {"connection": "con_1"})

    assert calls_named(api, "pause_connection") == [
        ("pause_connection", ("con_1",), {})
    ]
    assert "Paused" in message
    # Silently is the one way it must not fail: nothing will resume this now.
    assert "Could not open the ledger" in caplog.text


# ----------------------------------------------------------------------
# Which connection a name resolves to
# ----------------------------------------------------------------------


def test_an_id_is_used_directly_without_a_lookup(api, ledger_at):
    call("hookdeck_pause_connection", {"connection": "web_abc"})
    assert not calls_named(api, "list_connections")
    assert calls_named(api, "pause_connection") == [
        ("pause_connection", ("web_abc",), {})
    ]


def test_a_name_is_resolved_to_an_id_before_anything_is_paused(api, ledger_at):
    api.responses["list_connections"] = {"models": [{"id": "web_resolved"}]}
    call("hookdeck_pause_connection", {"connection": "github"})
    assert calls_named(api, "list_connections") == [
        ("list_connections", (), {"name": "github"})
    ]
    assert calls_named(api, "pause_connection") == [
        ("pause_connection", ("web_resolved",), {})
    ]


def test_a_name_matching_nothing_pauses_nothing(api, ledger_at):
    # Acting on "whatever came back first" when nothing came back is how a
    # tool pauses an unrelated connection.
    api.responses["list_connections"] = {"models": []}
    message = call("hookdeck_pause_connection", {"connection": "typo"})
    assert "No connection named 'typo'" in message
    assert not calls_named(api, "pause_connection")


@pytest.mark.parametrize("tool", ["hookdeck_pause_connection", "hookdeck_resume_connection"])
def test_a_missing_connection_is_refused_rather_than_guessed(api, tool):
    message = call(tool, {})
    assert "connection is required" in message
    assert api.calls == []


# ----------------------------------------------------------------------
# Error shaping
# ----------------------------------------------------------------------


def test_an_api_error_reaches_the_model_as_a_sentence(api):
    api.raises = HookdeckAPIError(429, "GET", "/events", "rate limited")
    message = call("hookdeck_list_failed_events")
    assert message.startswith("Hookdeck API error:")
    assert "429" in message and "rate limited" in message
    assert "Traceback" not in message


def test_an_unexpected_failure_is_also_caught(api):
    # A tool that raises takes the whole agent turn down with a traceback the
    # model cannot act on. Every handler is wrapped for this reason.
    api.raises = RuntimeError("something unforeseen")
    message = call("hookdeck_queue_status")
    assert message == "Hookdeck tool failed: something unforeseen"


def test_handlers_accept_no_arguments_at_all(api):
    # Hermes may dispatch a no-parameter tool with `None` rather than `{}`.
    assert tools.HANDLERS["hookdeck_queue_status"]() is not None


# ----------------------------------------------------------------------
# Running async work from a synchronous handler
# ----------------------------------------------------------------------


def test_tools_work_with_no_event_loop_running(api):
    api.responses["queue_depth"] = {"max_depth": 3}
    assert json.loads(call("hookdeck_queue_status"))["queue_depth"] == {"max_depth": 3}


async def test_tools_work_when_a_loop_is_already_running(api):
    # The worker-thread fallback. `asyncio.run` raises inside a running loop,
    # and a tool dispatched from async context is the normal case, not the
    # exotic one.
    api.responses["queue_depth"] = {"max_depth": 7}
    assert json.loads(call("hookdeck_queue_status"))["queue_depth"] == {"max_depth": 7}


async def test_a_failure_on_the_worker_thread_still_reaches_the_caller(api):
    # The thread swallows the exception into a box; if it were not re-raised,
    # the tool would silently return None from inside a running loop.
    api.raises = HookdeckAPIError(500, "GET", "/metrics/queue-depth", "boom")
    assert call("hookdeck_queue_status").startswith("Hookdeck API error:")


# ----------------------------------------------------------------------
# The read tools
# ----------------------------------------------------------------------


def test_failed_events_are_summarised_rather_than_dumped(api):
    api.responses["list_events"] = {
        "models": [
            {
                "id": "evt_1",
                "error_code": "TIMEOUT",
                "response_status": 500,
                "attempts": 3,
                "created_at": "2026-08-10T00:00:00Z",
                "body": {"enormous": "x" * 10_000},
            }
        ]
    }
    rows = json.loads(call("hookdeck_list_failed_events"))
    assert rows == [
        {
            "id": "evt_1",
            "error_code": "TIMEOUT",
            "response_status": 500,
            "attempts": 3,
            "created_at": "2026-08-10T00:00:00Z",
        }
    ]


def test_no_failures_says_so_in_words(api):
    api.responses["list_events"] = {"models": []}
    assert call("hookdeck_list_failed_events") == "No failed events."


@pytest.mark.parametrize(
    "payload", [None, [], "an error string", {}, {"models": None}, {"models": "nope"}]
)
def test_an_unexpected_response_shape_reads_as_no_results(api, payload):
    # `_models` is the only thing between a changed API response and a
    # traceback. Reporting nothing is wrong but safe; raising is neither.
    api.responses["list_events"] = payload
    assert call("hookdeck_list_failed_events") == "No failed events."


@pytest.mark.parametrize(
    "given, expected",
    [({}, 20), ({"limit": 5}, 5), ({"limit": 500}, 100), ({"limit": None}, 20)],
)
def test_the_page_size_is_capped(api, given, expected):
    call("hookdeck_list_failed_events", given)
    assert calls_named(api, "list_events")[0][2]["limit"] == expected


def test_listing_can_be_scoped_by_connection_and_time(api):
    call(
        "hookdeck_list_failed_events",
        {"connection_id": "web_1", "since": "2026-08-01T00:00:00Z"},
    )
    kwargs = calls_named(api, "list_events")[0][2]
    assert kwargs["webhook_id"] == "web_1"
    assert kwargs["created_at[gte]"] == "2026-08-01T00:00:00Z"
    assert kwargs["status"] == "FAILED"


def test_an_event_body_is_truncated_before_it_reaches_the_context(api):
    api.responses["get_event_raw_body"] = "x" * 50_000
    assert len(call("hookdeck_get_event_body", {"event_id": "evt_1"})) == 8000


def test_the_raw_body_envelope_is_unwrapped(api):
    # The endpoint answers {"body": "<text>"}. Returning that whole hands the
    # model an escaped string inside an envelope instead of the payload —
    # invisible in a transcript, because the model tidies it up when it
    # summarises.
    api.responses["get_event_raw_body"] = {
        "body": '{"kind":"charge.succeeded","amount":2000}'
    }
    assert call("hookdeck_get_event_body", {"event_id": "evt_1"}) == (
        '{"kind":"charge.succeeded","amount":2000}'
    )


def test_an_unwrapped_string_body_still_works(api):
    api.responses["get_event_raw_body"] = '{"kind":"already-plain"}'
    assert call("hookdeck_get_event_body", {"event_id": "evt_1"}) == (
        '{"kind":"already-plain"}'
    )


def test_only_the_exact_envelope_shape_is_unwrapped(api):
    # Measured: the endpoint always answers exactly {"body": "<string>"}. So a
    # dict shaped any other way is the payload, not the envelope — including
    # third-party JSON that happens to carry its own `body` field. Unwrapping
    # that would return a fragment of the event as though it were the whole
    # thing, which is worse than the envelope it replaced.
    api.responses["get_event_raw_body"] = {"body": "inner", "from": "acme"}
    assert json.loads(call("hookdeck_get_event_body", {"event_id": "evt_1"})) == {
        "body": "inner",
        "from": "acme",
    }

    api.responses["get_event_raw_body"] = {"body": {"kind": "structured"}}
    assert json.loads(call("hookdeck_get_event_body", {"event_id": "evt_1"})) == {
        "body": {"kind": "structured"}
    }


def test_a_structured_body_is_serialised(api):
    api.responses["get_event_raw_body"] = {"type": "charge.succeeded"}
    assert json.loads(call("hookdeck_get_event_body", {"event_id": "evt_1"})) == {
        "type": "charge.succeeded"
    }


@pytest.mark.parametrize("tool", ["hookdeck_get_event_body", "hookdeck_retry_event"])
def test_an_event_id_is_required(api, tool):
    assert call(tool, {"event_id": "   "}) == "event_id is required."
    assert api.calls == []


# ----------------------------------------------------------------------
# The write tools
# ----------------------------------------------------------------------


def test_retrying_one_event_names_it_back(api):
    assert "evt_1" in call("hookdeck_retry_event", {"event_id": " evt_1 "})
    assert calls_named(api, "retry_event") == [("retry_event", ("evt_1",), {})]


def test_bulk_retry_is_scoped_to_this_gateways_connections(api, ledger_at, monkeypatch):
    # `status: FAILED` alone matches the whole project, and a project usually
    # holds connections belonging to something else. An agent can call this
    # with no arguments, so the default must not redeliver their traffic.
    monkeypatch.setattr(
        "hookdeck.settings.load_hermes_config",
        lambda: {"gateway": {"platforms": {"hookdeck": {"extra": {
            "routes": {"github": {}, "stripe": {}}}}}}},
    )
    api.responses["list_connections"] = {"models": [{"id": "web_mine"}]}
    call("hookdeck_bulk_retry")

    sent = calls_named(api, "bulk_retry_events")[0][1][0]
    assert sent["status"] == "FAILED"
    assert sent["webhook_id"] == ["web_mine", "web_mine"]  # one per route
    # Resolved by name, the same way the dashboard decides what it may pause.
    assert [c[2]["name"] for c in calls_named(api, "list_connections")] == [
        "github", "stripe"
    ]


def test_bulk_retry_refuses_rather_than_widening_when_it_owns_nothing(
    api, ledger_at, monkeypatch
):
    # The dangerous fallback would be "no connections resolved, so retry
    # everything". Refuse and say so instead.
    monkeypatch.setattr(
        "hookdeck.settings.load_hermes_config",
        lambda: {"gateway": {"platforms": {"hookdeck": {"extra": {"routes": {}}}}}},
    )
    message = call("hookdeck_bulk_retry")
    assert "nothing this gateway owns" in message
    assert not calls_named(api, "bulk_retry_events")


def test_an_explicit_connection_overrides_the_default_scope(api, ledger_at):
    call("hookdeck_bulk_retry", {"connection_id": "web_explicit"})
    sent = calls_named(api, "bulk_retry_events")[0][1][0]
    assert sent["webhook_id"] == "web_explicit"
    assert not calls_named(api, "list_connections"), "no need to resolve ours"


def test_matching_no_events_reads_as_an_answer_not_a_failure(api, ledger_at):
    # The API refuses an empty batch with 422. Surfacing that as an API error
    # tells the model something is broken when the true answer is "nothing to
    # do" — and it is the common case on a healthy gateway.
    from hookdeck.api import HookdeckAPIError

    api.responses["list_connections"] = {"models": [{"id": "web_1"}]}
    api.raises_for = {"bulk_retry_events": HookdeckAPIError(
        422, "POST", "/bulk/events/retry",
        "The query filter for the batch operations does not include any events.",
    )}
    message = call("hookdeck_bulk_retry", {"connection_id": "web_1"})
    assert message == "No failed events matched — nothing to retry."


def test_a_bulk_retry_carries_its_scope_into_the_query(api):
    call(
        "hookdeck_bulk_retry",
        {"since": "2026-08-01T00:00:00Z", "connection_id": "web_1"},
    )
    assert calls_named(api, "bulk_retry_events")[0][1][0] == {
        "status": "FAILED",
        "created_at": {"gte": "2026-08-01T00:00:00Z"},
        "webhook_id": "web_1",
    }


def test_a_bulk_retry_result_cannot_flood_the_context(api):
    api.responses["bulk_retry_events"] = {"models": [{"id": f"evt_{i}"} for i in range(5000)]}
    assert len(call("hookdeck_bulk_retry")) < 1100


# ----------------------------------------------------------------------
# Registration
# ----------------------------------------------------------------------


def test_every_tool_is_registered_with_a_schema_and_a_handler():
    registered: list[dict] = []
    tools.register_tools(
        type("Ctx", (), {"register_tool": lambda _self, **kw: registered.append(kw)})()
    )
    assert {r["name"] for r in registered} == set(tools.SCHEMAS)
    for entry in registered:
        assert entry["toolset"] == tools.TOOLSET
        assert callable(entry["handler"])
        assert entry["schema"]["function"]["name"] == entry["name"]
        assert entry["description"]
        assert entry["emoji"]


def test_every_required_parameter_is_a_declared_parameter():
    # A required name absent from `properties` is a schema the model cannot
    # satisfy — it never learns the parameter exists.
    for name, schema in tools.SCHEMAS.items():
        params = schema["function"]["parameters"]
        missing = set(params["required"]) - set(params["properties"])
        assert not missing, f"{name} requires undeclared {missing}"


# ----------------------------------------------------------------------
# Queue status reports numbers, not page sizes
# ----------------------------------------------------------------------


def test_queue_status_reports_the_real_number_of_open_issues(api):
    # Counted from the dedicated endpoint. Counting from a listing would count
    # to whatever limit was asked for — a project with four open issues used
    # to be reported as one, and the model then guessed at what "1" meant.
    api.responses["count_issues"] = 4
    api.responses["list_events"] = {"models": []}
    status = json.loads(call("hookdeck_queue_status"))

    assert status["open_issues"] == 4
    assert calls_named(api, "count_issues") == [
        ("count_issues", (), {"status": "OPENED"})
    ]
    # The old page-size fields are gone, not merely renamed alongside.
    assert "open_issues_page_count" not in status
    assert "failed_events_page_count" not in status


def test_queue_status_counts_failed_events_exactly_when_it_can(api):
    api.responses["list_events"] = {"models": [{"id": f"evt_{i}"} for i in range(7)]}
    status = json.loads(call("hookdeck_queue_status"))
    assert status["failed_events"] == 7
    assert status["failed_events_is_at_least"] is False


def test_a_full_page_of_failures_is_flagged_as_a_floor(api):
    # Events have no count endpoint, so a full page means "at least this
    # many". Reporting it bare would let the model state a ceiling as a total.
    api.responses["list_events"] = {
        "models": [{"id": f"evt_{i}"} for i in range(tools.FAILED_SCAN_LIMIT)]
    }
    status = json.loads(call("hookdeck_queue_status"))
    assert status["failed_events"] == tools.FAILED_SCAN_LIMIT
    assert status["failed_events_is_at_least"] is True


def test_queue_status_asks_for_more_than_one_failure(api):
    api.responses["list_events"] = {"models": []}
    call("hookdeck_queue_status")
    assert calls_named(api, "list_events")[0][2]["limit"] == tools.FAILED_SCAN_LIMIT
