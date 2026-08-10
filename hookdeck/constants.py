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

# Matches the built-in webhook adapter's local-testing escape hatch, so the
# two platforms behave the same way when someone is poking at them with curl.
INSECURE_NO_AUTH = "INSECURE_NO_AUTH"

API_BASE_URL = "https://api.hookdeck.com/2025-07-01"


def header_name(suffix: str, prefix: str = DEFAULT_HEADER_PREFIX) -> str:
    """Build a Hookdeck header name from the project's configured prefix."""
    return f"{prefix.rstrip('-')}-{suffix}"
