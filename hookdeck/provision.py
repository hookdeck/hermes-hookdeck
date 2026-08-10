"""Building and applying Hookdeck configuration for a Hermes route.

``build_connection_payload`` is a pure function so the shape of what gets sent
to Hookdeck can be tested without a network. Everything else is a thin
coroutine over :class:`~hookdeck.api.HookdeckAPI`.

The rules attached to the connection are where most of the reliability lives:

* ``retry`` — exponential backoff, so a Hermes restart is survivable
* ``deduplicate`` — a provider that double-fires within the window costs one
  agent run, not two
* ``filter`` — drops uninteresting events before they reach an LLM, which is
  the one setting here with a directly measurable token cost saving
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

DEFAULT_RETRY_COUNT = 5
DEFAULT_RETRY_INTERVAL_MS = 30_000
DEFAULT_DEDUPE_WINDOW_MS = 60_000
DEFAULT_GROUP_RATE = 1
DEFAULT_GROUP_RATE_PERIOD = "minute"

# `auth_type` never travels alone: the API rejects a destination config with
# `destination.config.auth is required` if it does, even for HOOKDECK_SIGNATURE
# whose auth object is empty. The OpenAPI schema does not mark it required, so
# this is only discoverable by making the call.
HOOKDECK_SIGNATURE_AUTH = {"auth_type": "HOOKDECK_SIGNATURE", "auth": {}}

# Hookdeck source types whose event name arrives in a header rather than the
# body. Used to turn a route's ``events`` list into a gateway-side filter.
_EVENT_HEADER_BY_TYPE = {
    "GITHUB": "x-github-event",
    "GITLAB": "x-gitlab-event",
    "SHOPIFY": "x-shopify-topic",
}


# Destination-level rate limiting accepts `concurrent`; delivery groups do not.
# DeliveryGroupRateLimitPeriod is second|minute|hour, so per-group *concurrency*
# — one in-flight run per customer — is not expressible. Groups throttle by
# rate only. Sending `concurrent` inside delivery_groups is rejected by the API.
DESTINATION_RATE_PERIODS = ("second", "minute", "hour", "concurrent")
GROUP_RATE_PERIODS = ("second", "minute", "hour")


def _http_destination_config(
    url: str,
    *,
    rate_limit: Optional[int],
    rate_limit_period: str,
    delivery_group_key: str,
    group_rate: int,
    group_rate_period: str,
) -> dict[str, Any]:
    config: dict[str, Any] = {
        "url": url,
        # Hookdeck signs its delivery so the adapter has exactly one signature
        # scheme to verify, whatever the upstream provider used.
        **HOOKDECK_SIGNATURE_AUTH,
    }
    if rate_limit:
        if rate_limit_period not in DESTINATION_RATE_PERIODS:
            raise ValueError(
                f"rate_limit_period must be one of {DESTINATION_RATE_PERIODS}"
            )
        config["rate_limit"] = rate_limit
        config["rate_limit_period"] = rate_limit_period
    if delivery_group_key:
        if group_rate_period not in GROUP_RATE_PERIODS:
            raise ValueError(
                "delivery groups do not support "
                f"rate_limit_period={group_rate_period!r}; Hookdeck allows "
                f"{GROUP_RATE_PERIODS}. Cap total concurrency with the "
                "destination-level rate_limit instead."
            )
        config["delivery_groups"] = {
            "key": delivery_group_key,
            "rate_limit": group_rate,
            "rate_limit_period": group_rate_period,
        }
    return config


def build_connection_payload(
    *,
    name: str,
    source_name: str,
    source_type: str = "WEBHOOK",
    destination_name: str = "",
    mode: str = "cli",
    path: str = "/hookdeck",
    url: str = "",
    events: Optional[list[str]] = None,
    event_path: str = "",
    rate_limit: Optional[int] = None,
    rate_limit_period: str = "concurrent",
    delivery_group_key: str = "",
    group_rate: int = DEFAULT_GROUP_RATE,
    group_rate_period: str = DEFAULT_GROUP_RATE_PERIOD,
    retry_count: int = DEFAULT_RETRY_COUNT,
    retry_interval_ms: int = DEFAULT_RETRY_INTERVAL_MS,
    dedupe_window_ms: Optional[int] = DEFAULT_DEDUPE_WINDOW_MS,
    source_secret: str = "",
) -> dict[str, Any]:
    """Assemble the ``PUT /connections`` body for one Hermes route.

    Upsert-by-name is what makes ``hermes hookdeck setup`` safe to re-run: the
    same route always maps to the same connection.
    """
    source_type = (source_type or "WEBHOOK").upper()
    destination_name = destination_name or f"hermes-{name}"

    source: dict[str, Any] = {"name": source_name, "type": source_type}
    if source_secret and source_type in {"WEBHOOK", "HTTP"}:
        # Only the generic source types take a caller-supplied HMAC config;
        # named platform types carry their own verification shape, configured
        # with the provider's own secret in the Hookdeck dashboard.
        source["config"] = {
            "auth_type": "HMAC",
            "auth": {
                "algorithm": "sha256",
                "encoding": "hex",
                "header_key": "x-signature",
                "webhook_secret_key": source_secret,
            },
        }

    if mode == "cli":
        destination = {
            "name": destination_name,
            "type": "CLI",
            "config": {"path": path, **HOOKDECK_SIGNATURE_AUTH},
        }
    else:
        if not url:
            raise ValueError("push mode needs a destination URL")
        destination = {
            "name": destination_name,
            "type": "HTTP",
            "config": _http_destination_config(
                url,
                rate_limit=rate_limit,
                rate_limit_period=rate_limit_period,
                delivery_group_key=delivery_group_key,
                group_rate=group_rate,
                group_rate_period=group_rate_period,
            ),
        }

    # Narrowing from Hookdeck's default (retry any non-2xx) so a 401 from a
    # secret mismatch or a 404 from a missing route fails fast instead of
    # burning 50 attempts on something no retry can fix.
    response_status_codes = ["500-599"]
    still_missing = uncovered_statuses(response_status_codes)
    if still_missing:  # pragma: no cover - guards against editing the list above
        raise ValueError(
            f"retry rule would not cover statuses the adapter emits: {still_missing}"
        )

    rules: list[dict[str, Any]] = [
        {
            "type": "retry",
            "strategy": "exponential",
            "count": retry_count,
            "interval": retry_interval_ms,
            "response_status_codes": response_status_codes,
        }
    ]
    if dedupe_window_ms:
        rules.append({"type": "deduplicate", "window": dedupe_window_ms})

    event_filter = build_event_filter(events or [], source_type, event_path)
    if event_filter:
        rules.append(event_filter)

    return {
        "name": name,
        "source": source,
        "destination": destination,
        "rules": rules,
    }


def build_event_filter(
    events: list[str], source_type: str, event_path: str
) -> Optional[dict[str, Any]]:
    """Turn a route's ``events`` list into a Hookdeck filter rule.

    Returns ``None`` when the event name's location is unknown — a wrong filter
    silently discards traffic, so the adapter's own ``events`` check is left to
    do the work instead. Set ``event_path`` on the route to enable gateway-side
    filtering for providers that carry the type in the body.
    """
    if not events:
        return None
    if event_path:
        node: Any = {"$in": list(events)}
        for part in reversed(event_path.split(".")):
            node = {part: node}
        return {"type": "filter", "body": node}
    header = _EVENT_HEADER_BY_TYPE.get(source_type.upper())
    if header:
        return {"type": "filter", "headers": {header: {"$in": list(events)}}}
    return None


# HTTP statuses the adapter itself emits that must be retried: 503 is admission
# control, 500 is a failed run in sync mode, 502 is a rejected direct delivery.
# A retry rule that does not cover these turns backpressure into silent data
# loss — the event is deferred and then never comes back.
ADAPTER_RETRYABLE_STATUSES = (500, 502, 503)


def _code_expression_matches(expression: str, status: int) -> Optional[bool]:
    """Evaluate one Hookdeck retry-rule code expression against *status*.

    Returns True/False for a positive match, or None when the expression is an
    exclusion that does not apply. Exclusions (``!401``) return False when they
    do apply, so a caller folding results with ``any`` needs the exclusions
    checked first — which :func:`uncovered_statuses` does.
    """
    expression = str(expression).strip()
    if expression.startswith("!"):
        return False if expression[1:] == str(status) else None
    if "-" in expression:
        low, _, high = expression.partition("-")
        return int(low) <= status <= int(high)
    for operator in (">=", "<=", ">", "<", "="):
        if expression.startswith(operator):
            bound = int(expression[len(operator) :])
            return {
                ">=": status >= bound,
                "<=": status <= bound,
                ">": status > bound,
                "<": status < bound,
                "=": status == bound,
            }[operator]
    return status == int(expression)


def uncovered_statuses(
    codes: Optional[list[str]], statuses: tuple[int, ...] = ADAPTER_RETRYABLE_STATUSES
) -> list[int]:
    """Which of *statuses* a retry rule's ``response_status_codes`` misses.

    An empty or absent list means Hookdeck's default — retry any non-2xx — so
    nothing is uncovered.
    """
    if not codes:
        return []
    missing = []
    for status in statuses:
        excluded = any(str(c).strip() == f"!{status}" for c in codes)
        matched = any(_code_expression_matches(c, status) is True for c in codes)
        if excluded or not matched:
            missing.append(status)
    return missing


def routes_from_config(config: Mapping[str, Any]) -> dict[str, dict]:
    """Extract ``platforms.hookdeck.extra.routes`` from a parsed config.yaml."""
    gateway = config.get("gateway") or {}
    platforms = gateway.get("platforms") or config.get("platforms") or {}
    hookdeck = platforms.get("hookdeck") or {}
    extra = hookdeck.get("extra") or {}
    return dict(extra.get("routes") or {})


def summarise_payload(payload: Mapping[str, Any]) -> str:
    """One-line human summary of what a setup call is about to create."""
    source = payload.get("source") or {}
    destination = payload.get("destination") or {}
    rules = [r.get("type") for r in payload.get("rules") or []]
    return (
        f"{payload.get('name')}: {source.get('type')} source '{source.get('name')}' "
        f"-> {destination.get('type')} destination '{destination.get('name')}' "
        f"[rules: {', '.join(str(r) for r in rules) or 'none'}]"
    )
