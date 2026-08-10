from __future__ import annotations

from hookdeck import routing

ROUTES = {
    "github-prs": {"source": "github"},
    "stripe": {"source": "stripe-live"},
    "stripe-test": {"source": "stripe-sandbox"},
}


def test_the_path_names_the_route():
    assert routing.resolve(ROUTES, path_tail="stripe")[0] == "stripe"


def test_a_forwarded_provider_path_still_names_the_route():
    # Hookdeck appends the source request's own path unless
    # path_forwarding_disabled is set, so the route is the first segment only.
    name, route = routing.resolve(ROUTES, path_tail="stripe/events/v2")
    assert name == "stripe"
    assert route is ROUTES["stripe"]


def test_a_similar_name_is_not_swallowed():
    # Segment matching, not string prefix: `stripe` must not claim traffic
    # addressed to `stripe-test`.
    assert routing.resolve(ROUTES, path_tail="stripe-test/x")[0] == "stripe-test"


def test_the_source_name_resolves_a_route_when_the_path_does_not():
    name, route = routing.resolve(ROUTES, source_name="github")
    assert name == "github-prs"
    assert route is ROUTES["github-prs"]


def test_a_route_named_after_the_source_also_matches():
    routes = {"shopify": {}}
    assert routing.resolve(routes, source_name="shopify")[0] == "shopify"


def test_a_default_route_catches_what_nothing_else_claims():
    routes = {"a": {"source": "sa"}, "default": {}}
    assert routing.resolve(routes, source_name="unknown")[0] == "default"


def test_a_lone_route_catches_everything():
    routes = {"only": {}}
    assert routing.resolve(routes, source_name="whatever")[0] == "only"


def test_nothing_matching_returns_no_route():
    # The caller answers 404 rather than guessing: a wrong guess runs an agent
    # on someone else's payload.
    name, route = routing.resolve(ROUTES, source_name="shopify")
    assert route is None
    assert name == "shopify"


def test_an_explicit_body_path_names_the_event():
    assert (
        routing.event_type(
            {"data": {"kind": "dispute"}},
            route={"event_path": "data.kind"},
            headers={},
        )
        == "dispute"
    )


def test_provider_headers_name_the_event():
    assert (
        routing.event_type({}, route={}, headers={"X-GitHub-Event": "pull_request"})
        == "pull_request"
    )


def test_conventional_body_keys_are_a_last_resort():
    assert routing.event_type({"type": "charge.succeeded"}, route={}, headers={}) == (
        "charge.succeeded"
    )


def test_an_unidentifiable_event_is_named_unknown():
    # A configured `events` list will then refuse it, deliberately, rather than
    # letting an unidentified event through a filter meant to be selective.
    assert routing.event_type({"nothing": "useful"}, route={}, headers={}) == "unknown"


def test_each_route_gets_a_tunnel_and_a_shared_source_fills_the_gaps():
    assert routing.tunnel_plan(ROUTES) == {
        "github-prs": "github",
        "stripe": "stripe-live",
        "stripe-test": "stripe-sandbox",
    }
    assert routing.tunnel_plan({"orders": {}}, "shopify") == {"orders": "shopify"}
    assert routing.tunnel_plan({"orders": {}}) == {}
