from __future__ import annotations

import httpx
import pytest

from hookdeck.api import HookdeckAPI, HookdeckAPIError, _clean_params
from hookdeck.constants import (
    API_KEY_ENV,
    CLI_API_KEY_ENV,
)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_queue_depth_sends_the_nested_params_the_endpoint_requires():
    # /metrics/queue-depth looks parameterless and answers 422 without
    # date_range and measures. Both are bracket-encoded; a JSON-encoded object
    # is rejected with "must be of type object", which reads like the opposite.
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = request.url.params
        return httpx.Response(200, json={"data": []})

    api = HookdeckAPI("key", client=_client(handler))
    await api.queue_depth()

    params = seen["params"]
    assert params["date_range[start]"].endswith("Z")
    assert params["date_range[end]"].endswith("Z")
    # Repeated key — the whole reason params cannot be a plain dict.
    assert params.get_list("measures[]") == ["max_depth", "max_age"]


async def test_only_max_depth_and_max_age_are_requested_by_default():
    # The API rejects anything else: "measures[0] must be one of
    # [max_depth, max_age]".
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["measures"] = request.url.params.get_list("measures[]")
        return httpx.Response(200, json={})

    api = HookdeckAPI("key", client=_client(handler))
    await api.queue_depth(measures=["max_depth"])
    assert seen["measures"] == ["max_depth"]


def test_clean_params_preserves_repeated_keys():
    assert _clean_params(None) is None
    assert _clean_params({"a": 1, "b": None, "c": ""}) == {"a": 1}
    assert _clean_params([("m", "x"), ("m", "y"), ("n", None)]) == [("m", "x"), ("m", "y")]


async def test_a_missing_api_key_fails_before_any_request(monkeypatch):
    # Cleared explicitly: the client falls back to the environment, so a
    # developer with a key exported would otherwise not be testing this at all.
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    monkeypatch.delenv(CLI_API_KEY_ENV, raising=False)
    api = HookdeckAPI("")
    with pytest.raises(HookdeckAPIError, match=API_KEY_ENV):
        await api.list_events()


async def test_the_cli_api_key_variable_is_a_first_class_fallback(monkeypatch):
    # Not deprecated: HOOKDECK_API_KEY is what the Hookdeck CLI itself reads,
    # and this adapter passes it to the `hookdeck listen` subprocess. Demanding
    # a second name for one secret would be worse than sharing the convention.
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    monkeypatch.setenv(CLI_API_KEY_ENV, "key_from_the_cli_convention")
    assert HookdeckAPI().api_key == "key_from_the_cli_convention"


async def test_the_namespaced_api_key_wins(monkeypatch):
    monkeypatch.setenv(API_KEY_ENV, "key_namespaced")
    monkeypatch.setenv(CLI_API_KEY_ENV, "key_shared")
    assert HookdeckAPI().api_key == "key_namespaced"


async def test_non_2xx_carries_the_status_and_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, text='{"data":["measures is required"]}')

    api = HookdeckAPI("key", client=_client(handler))
    with pytest.raises(HookdeckAPIError) as exc:
        await api.list_events()
    assert exc.value.status == 422
    assert "measures is required" in exc.value.body


async def test_a_transport_failure_is_raised_as_an_api_error():
    # Callers catch HookdeckAPIError. A raw httpx.ConnectError would sail past
    # the bounded retry loop written for exactly this — the "network blip".
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    api = HookdeckAPI("key", client=_client(handler))
    with pytest.raises(HookdeckAPIError) as exc:
        await api.list_events()
    assert exc.value.status == 0
    assert "ConnectError" in exc.value.body


async def test_a_timeout_is_raised_as_an_api_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    api = HookdeckAPI("key", client=_client(handler))
    with pytest.raises(HookdeckAPIError):
        await api.list_events()


async def test_bulk_retry_wraps_its_filters_in_a_query_object():
    """The filters belong inside `query`, not at the top level.

    Sent flat, the API answers 500 FATAL_ERROR rather than 400 — so a wrong
    shape is indistinguishable from Hookdeck being down, and the agent
    reported exactly that. Every other test fakes the client and asserts the
    dict handed to this method, which is why none of them saw the wire.
    """
    import json as _json

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = _json.loads(request.content)
        return httpx.Response(200, json={"id": "bch_1", "estimated_count": 3})

    api = HookdeckAPI("key", client=_client(handler))
    await api.bulk_retry_events({"status": "FAILED", "webhook_id": "web_1"})

    assert seen["body"] == {"query": {"status": "FAILED", "webhook_id": "web_1"}}
    assert "status" not in seen["body"], "filters must not sit at the top level"
