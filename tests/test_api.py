from __future__ import annotations

import httpx
import pytest

from hookdeck.api import HookdeckAPI, HookdeckAPIError, _clean_params


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


async def test_a_missing_api_key_fails_before_any_request():
    api = HookdeckAPI("")
    with pytest.raises(HookdeckAPIError, match="HOOKDECK_API_KEY"):
        await api.list_events()


async def test_non_2xx_carries_the_status_and_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, text='{"data":["measures is required"]}')

    api = HookdeckAPI("key", client=_client(handler))
    with pytest.raises(HookdeckAPIError) as exc:
        await api.list_events()
    assert exc.value.status == 422
    assert "measures is required" in exc.value.body
