"""Deciding which configured route a delivery belongs to, and what it is.

Pure functions over plain data — no request object, no adapter state — so the
matching rules can be read and tested on their own.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .payload import dig

Routes = Mapping[str, dict]

# Providers that name the event in a header rather than the body. Hookdeck
# forwards the original request headers, so these still arrive intact.
_EVENT_HEADERS = ("X-GitHub-Event", "X-GitLab-Event", "X-Shopify-Topic")

# Body keys worth trying when nothing else identifies the event.
_EVENT_BODY_KEYS = ("event_type", "type", "event", "topic")

UNMATCHED = "(unmatched)"


def route_name_from_path(path_tail: str) -> str:
    """The route named by the URL, ignoring any provider path after it.

    Hookdeck appends the source request's own path to the destination path
    unless ``path_forwarding_disabled`` is set, so ``/hookdeck/stripe`` and
    ``/hookdeck/stripe/events/v2`` both name the ``stripe`` route.

    Matching the first *segment* rather than a string prefix is what stops
    route ``stripe`` claiming a delivery meant for ``stripe-test``.
    """
    return path_tail.split("/", 1)[0] if path_tail else ""


def resolve(
    routes: Routes, *, path_tail: str = "", source_name: str = ""
) -> tuple[str, dict | None]:
    """Pick the route for a delivery, most explicit signal first.

    Returns the route name and its config, or the name and ``None`` when
    nothing matches — the caller answers 404 rather than guessing, because a
    wrong guess runs an agent on someone else's payload.
    """
    named_in_path = route_name_from_path(path_tail)
    if named_in_path:
        return named_in_path, routes.get(named_in_path)

    if source_name:
        for name, route in routes.items():
            if str(route.get("source", "")) == source_name:
                return name, route
        if source_name in routes:
            return source_name, routes[source_name]

    if "default" in routes:
        return "default", routes["default"]
    if len(routes) == 1:
        only = next(iter(routes))
        return only, routes[only]

    return source_name or UNMATCHED, None


def event_type(
    payload: Any, *, route: Mapping[str, Any], headers: Mapping[str, str]
) -> str:
    """Name the event, for the route's ``events`` filter to match against.

    ``event_path`` on the route wins, since it is the only signal an operator
    states explicitly; then the provider headers; then a few conventional body
    keys. ``"unknown"`` when none of that finds anything — which a configured
    ``events`` list will then refuse, deliberately, rather than letting an
    unidentified event through a filter meant to be selective.
    """
    configured_path = route.get("event_path")
    if configured_path:
        value = dig(payload, str(configured_path))
        if value is not None:
            return str(value)

    for header in _EVENT_HEADERS:
        value = headers.get(header)
        if value:
            return value

    if isinstance(payload, dict):
        for key in _EVENT_BODY_KEYS:
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value

    return "unknown"


def tunnel_plan(routes: Routes, default_source: str = "") -> dict[str, str]:
    """Map route name -> Hookdeck source, one entry per CLI tunnel.

    ``hookdeck listen`` forwards exactly one source, so each route needs its
    own process. Routes sharing a source still get one each: the CLI path — and
    therefore the route :func:`resolve` picks — differs per route.
    """
    plan: dict[str, str] = {}
    for name, route in routes.items():
        source = str(route.get("source") or "") or default_source
        if source:
            plan[name] = source
    return plan
