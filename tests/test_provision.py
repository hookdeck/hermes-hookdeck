from __future__ import annotations

import pytest

from hookdeck.provision import (
    build_connection_payload,
    build_event_filter,
    routes_from_config,
    summarise_payload,
    uncovered_statuses,
)


def _rules_by_type(payload) -> dict:
    return {rule["type"]: rule for rule in payload["rules"]}


def test_cli_mode_builds_a_cli_destination():
    payload = build_connection_payload(
        name="github-prs", source_name="github", source_type="GITHUB", path="/hookdeck/github-prs"
    )
    assert payload["destination"]["type"] == "CLI"
    assert payload["destination"]["config"]["path"] == "/hookdeck/github-prs"
    # Hookdeck signs CLI deliveries too, so the adapter verifies in both modes.
    assert payload["destination"]["config"]["auth_type"] == "HOOKDECK_SIGNATURE"


def test_push_mode_sets_rate_limit_and_delivery_groups():
    payload = build_connection_payload(
        name="stripe",
        source_name="stripe",
        source_type="STRIPE",
        mode="push",
        url="https://agent.example.com/hookdeck/stripe",
        rate_limit=2,
        rate_limit_period="concurrent",
        delivery_group_key="body.data.object.customer",
    )
    config = payload["destination"]["config"]
    assert config["url"].endswith("/hookdeck/stripe")
    # `concurrent` is valid at destination level, and this is the only place
    # Hookdeck can cap in-flight agent runs for us.
    assert config["rate_limit"] == 2
    assert config["rate_limit_period"] == "concurrent"
    assert config["delivery_groups"]["key"] == "body.data.object.customer"


def test_push_mode_without_a_url_is_rejected():
    with pytest.raises(ValueError):
        build_connection_payload(name="x", source_name="x", mode="push")


def test_retry_rule_covers_the_backpressure_response():
    payload = build_connection_payload(name="x", source_name="x")
    retry = _rules_by_type(payload)["retry"]
    assert retry["strategy"] == "exponential"
    # 503 is the adapter's "at capacity" answer; if it were not retried,
    # admission control would silently drop events instead of deferring them.
    assert "500-599" in retry["response_status_codes"]
    # 401 from a secret mismatch and 404 from a missing route are not retried:
    # no retry can fix either, and Hookdeck's default of "any non-2xx" would
    # spend 50 attempts finding that out.
    assert uncovered_statuses(retry["response_status_codes"], (401, 404)) == [401, 404]


def test_dedupe_rule_is_added_by_default_and_can_be_disabled():
    assert "deduplicate" in _rules_by_type(build_connection_payload(name="x", source_name="x"))
    payload = build_connection_payload(name="x", source_name="x", dedupe_window_ms=None)
    assert "deduplicate" not in _rules_by_type(payload)


def test_event_filter_uses_the_provider_header_when_known():
    rule = build_event_filter(["pull_request"], "GITHUB", "")
    assert rule == {"type": "filter", "headers": {"x-github-event": {"$in": ["pull_request"]}}}


def test_event_filter_uses_a_body_path_when_configured():
    rule = build_event_filter(["charge.succeeded"], "STRIPE", "data.type")
    assert rule == {
        "type": "filter",
        "body": {"data": {"type": {"$in": ["charge.succeeded"]}}},
    }


def test_event_filter_is_omitted_when_the_location_is_unknown():
    # Guessing wrong here would silently discard traffic, so the adapter's own
    # events check is left to do the filtering instead.
    assert build_event_filter(["charge.succeeded"], "STRIPE", "") is None
    assert build_event_filter([], "GITHUB", "") is None


def test_generic_sources_accept_an_hmac_config():
    payload = build_connection_payload(
        name="x", source_name="x", source_type="WEBHOOK", source_secret="s3cr3t"
    )
    assert payload["source"]["config"]["auth"]["webhook_secret_key"] == "s3cr3t"

    named = build_connection_payload(
        name="x", source_name="x", source_type="STRIPE", source_secret="s3cr3t"
    )
    # Named platform types carry their own verification shape — inventing an
    # HMAC config for them would produce a source that rejects real traffic.
    assert "config" not in named["source"]


def test_routes_are_read_from_the_gateway_config_block():
    config = {
        "gateway": {
            "platforms": {"hookdeck": {"extra": {"routes": {"a": {"events": ["x"]}}}}}
        }
    }
    assert routes_from_config(config) == {"a": {"events": ["x"]}}
    assert routes_from_config({}) == {}


def test_summary_mentions_source_destination_and_rules():
    summary = summarise_payload(build_connection_payload(name="x", source_name="src"))
    assert "src" in summary and "CLI" in summary and "retry" in summary


# ----------------------------------------------------------------------
# Delivery groups and retry-rule coverage
# ----------------------------------------------------------------------


def test_delivery_groups_reject_the_concurrent_period():
    # DeliveryGroupRateLimitPeriod is second|minute|hour. Sending `concurrent`
    # here is rejected by the API, so it has to fail loudly at build time.
    with pytest.raises(ValueError, match="delivery groups do not support"):
        build_connection_payload(
            name="x",
            source_name="x",
            mode="push",
            url="https://example.com/x",
            delivery_group_key="body.customer",
            group_rate_period="concurrent",
        )


def test_delivery_groups_use_a_rate_not_a_concurrency_cap():
    payload = build_connection_payload(
        name="x",
        source_name="x",
        mode="push",
        url="https://example.com/x",
        delivery_group_key="body.customer",
        group_rate=2,
        group_rate_period="minute",
    )
    assert payload["destination"]["config"]["delivery_groups"] == {
        "key": "body.customer",
        "rate_limit": 2,
        "rate_limit_period": "minute",
    }


def test_destination_level_rate_limit_still_accepts_concurrent():
    payload = build_connection_payload(
        name="x",
        source_name="x",
        mode="push",
        url="https://example.com/x",
        rate_limit=2,
        rate_limit_period="concurrent",
    )
    assert payload["destination"]["config"]["rate_limit_period"] == "concurrent"


def test_an_invalid_destination_period_is_rejected():
    with pytest.raises(ValueError, match="rate_limit_period must be"):
        build_connection_payload(
            name="x",
            source_name="x",
            mode="push",
            url="https://example.com/x",
            rate_limit=1,
            rate_limit_period="fortnight",
        )


def test_the_default_retry_rule_covers_every_status_the_adapter_emits():
    rule = _rules_by_type(build_connection_payload(name="x", source_name="x"))["retry"]
    assert uncovered_statuses(rule["response_status_codes"]) == []


def test_uncovered_statuses_reads_hookdecks_code_expressions():
    assert uncovered_statuses(None) == []          # absent means "any non-2xx"
    assert uncovered_statuses([]) == []
    assert uncovered_statuses(["500-599"]) == []
    assert uncovered_statuses([">499"]) == []
    assert uncovered_statuses(["500", "502"]) == [503]
    assert uncovered_statuses(["400-499"]) == [500, 502, 503]
    # An explicit exclusion beats a range that would otherwise match.
    assert uncovered_statuses(["500-599", "!503"]) == [503]
