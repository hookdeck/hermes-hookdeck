"""Shared constants: Hookdeck wire headers and adapter defaults.

The ``x-hookdeck-`` prefix is configurable per Hookdeck project (Project
Settings → Headers), so every lookup goes through :func:`header_name` rather
than hardcoding the literal. Projects that have not customised the prefix get
the documented defaults.
"""

from __future__ import annotations

PLATFORM_NAME = "hookdeck"

DEFAULT_HEADER_PREFIX = "x-hookdeck"

# Suffixes appended to the configured prefix. Hookdeck documents these as
# X-Hookdeck-Signature, X-Hookdeck-EventID, X-Hookdeck-Attempt-Count, etc.
SIGNATURE = "signature"
SIGNATURE_2 = "signature-2"
EVENT_ID = "eventid"
REQUEST_ID = "requestid"
ATTEMPT_COUNT = "attempt-count"
ATTEMPT_TRIGGER = "attempt-trigger"
# Absent or empty means this is the final automatic attempt.
WILL_RETRY_AFTER = "will-retry-after"
SOURCE_NAME = "source-name"
CONNECTION_NAME = "connection-name"
DESTINATION_NAME = "destination-name"

# Adapter defaults. Deliberately conservative: two concurrent agent runs is
# already more parallel LLM spend than most people expect from a webhook.
DEFAULT_PORT = 3579
DEFAULT_PATH = "/hookdeck"
DEFAULT_ACK_MODE = "async_retry"
DEFAULT_MAX_CONCURRENT = 2
DEFAULT_MAX_AGENT_RETRIES = 3
DEFAULT_MAX_BODY_BYTES = 1_048_576
DEFAULT_RETRY_AFTER_SECONDS = 30
# Deferrals of one event before the 503 stops carrying Retry-After. Beyond
# this the saturation is not transient, and a fixed short interval would
# spend the automatic-retry budget in minutes instead of days.
DEFAULT_DEFER_ATTEMPT_LIMIT = 5
DEFAULT_LEDGER_TTL_SECONDS = 7 * 24 * 3600
DEFAULT_SYNC_TIMEOUT_SECONDS = 25
DEFAULT_RUN_TIMEOUT_SECONDS = 900

# How the adapter reports an agent run's outcome back to Hookdeck.
#
#   async_retry — ack 202 as soon as the event is admitted, run the agent in the
#                 background, and call POST /events/{id}/retry if the run fails.
#                 Hookdeck's queue stays the source of truth for retries without
#                 the delivery ever being held open.
#   sync        — hold the HTTP response until the run finishes (bounded by
#                 sync_timeout_seconds) so the event's status in Hookdeck is the
#                 agent's real outcome and Hookdeck's own retry rules apply.
#
# Backpressure is not a mode: max_concurrent is enforced in both, and an event
# that arrives over the limit gets 503 + Retry-After so it stays queued.
ACK_MODES = ("async_retry", "sync")

# Every HTTP status the adapter answers a Hookdeck delivery with, and whether a
# redelivery of THAT EXACT EVENT could still succeed. This is the single place
# the question is decided: the provisioned retry rule is derived from it, and
# emitting an undeclared status raises. Stating the two halves separately is
# what let an earlier version answer 401 and 404 while provisioning a rule that
# retried neither — recoverable events, silently discarded.
#
# The test is "can a retry of this stored request ever succeed", and it is
# answered against *this adapter's* operator surface, not the status code in
# the abstract: 413 is retryable here only because max_body_bytes is operator
# config. A host that hardcodes its body limit would answer differently.
EMITTED_STATUS_RETRYABLE = {
    200: False,  # duplicate, ignored, delivered — nothing to retry
    202: False,  # accepted
    400: False,  # unparseable body — part of the stored request, never parses
    401: True,   # operator fixes destination auth; Hookdeck signs at delivery
    404: True,   # operator adds or enables the route; same event then matches
    413: True,   # operator raises max_body_bytes; same event then fits
    500: True,   # failed run in sync mode
    502: True,   # direct delivery rejected downstream
    503: True,   # admission control — capacity frees up
}

# Statuses a provisioned retry rule must cover. Derived, never hand-written.
RETRYABLE_STATUSES = tuple(
    sorted(status for status, retry in EMITTED_STATUS_RETRYABLE.items() if retry)
)


def assert_declared_status(status: int) -> int:
    """Guard every delivery response against an undeclared status.

    Adding a status without deciding its retryability is the drift this whole
    table exists to prevent, so it fails at the call site rather than in
    production six weeks later.
    """
    if status not in EMITTED_STATUS_RETRYABLE:
        raise AssertionError(
            f"[hookdeck] status {status} is not declared in "
            "EMITTED_STATUS_RETRYABLE. Decide whether a redelivery of the same "
            "event could succeed, add it there, and re-run "
            "`hermes hookdeck setup` so the connection's retry rule matches."
        )
    return status

# Matches the built-in webhook adapter's local-testing escape hatch, so the
# two platforms behave the same way when someone is poking at them with curl.
INSECURE_NO_AUTH = "INSECURE_NO_AUTH"

API_BASE_URL = "https://api.hookdeck.com/2025-07-01"


def header_name(suffix: str, prefix: str = DEFAULT_HEADER_PREFIX) -> str:
    """Build a Hookdeck header name from the project's configured prefix."""
    return f"{prefix.rstrip('-')}-{suffix}"
