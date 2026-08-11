"""Shared constants: Hookdeck wire headers and adapter defaults.

The ``x-hookdeck-`` prefix is configurable per Hookdeck project (Project
Settings → Headers), so every lookup goes through :func:`header_name` rather
than hardcoding the literal. Projects that have not customised the prefix get
the documented defaults.
"""

from __future__ import annotations

import os

PLATFORM_NAME = "hookdeck"

DEFAULT_HEADER_PREFIX = "x-hookdeck"

# ----------------------------------------------------------------------
# Environment variables
# ----------------------------------------------------------------------
#
# Namespaced to the Event Gateway. Hookdeck's platform is more than one product
# — Outpost points the other way, at outbound delivery — and a bare `HOOKDECK_`
# prefix claims the whole namespace for whichever integration got there first.
# `HOOKDECK_EG_` says which product the value configures.
ENV_PREFIX = "HOOKDECK_EG_"

WEBHOOK_SECRET_ENV = f"{ENV_PREFIX}WEBHOOK_SECRET"
MODE_ENV = f"{ENV_PREFIX}MODE"
PORT_ENV = f"{ENV_PREFIX}PORT"
PATH_ENV = f"{ENV_PREFIX}PATH"
SOURCE_ENV = f"{ENV_PREFIX}SOURCE"
ALLOWED_USERS_ENV = f"{ENV_PREFIX}ALLOWED_USERS"
ALLOW_ALL_USERS_ENV = f"{ENV_PREFIX}ALLOW_ALL_USERS"

# The API key is deliberately NOT renamed the same way. `HOOKDECK_API_KEY` is
# the Hookdeck CLI's own documented variable — `hookdeck ci --api-key` defaults
# to it, and this adapter passes it through to the `hookdeck listen` subprocess
# it spawns. Forcing a second name for the same secret would mean setting two
# variables to one value. So the namespaced name wins if present, and the
# ecosystem-wide one is a first-class fallback rather than a deprecated one.
API_KEY_ENV = f"{ENV_PREFIX}API_KEY"
#: Read when the namespaced name is unset, and the name the CLI subprocess is
#: always given, whichever of the two the value came from.
CLI_API_KEY_ENV = "HOOKDECK_API_KEY"

# Which Hookdeck project the API key should act on.
#
# Optional today and load-bearing soon. A Hookdeck API key is currently scoped
# to one project, so the key implies the project and nothing has to say it.
# Organisation-level keys are coming, and one of those can reach several
# projects — at which point "the project" is no longer implied by the
# credential and has to be stated.
#
# Setting it now is also a safety improvement rather than only future-proofing:
# the dashboard decides which connections this gateway may pause by matching
# *names* against the configured routes, and an unscoped org key would let a
# same-named connection in an unrelated project match.
PROJECT_ID_ENV = f"{ENV_PREFIX}PROJECT_ID"
#: Header that selects the project, sent by the Hookdeck CLI too. Hookdeck's
#: API still calls a project a "team" on the wire; the operator-facing name has
#: been "project" for a while, so the config says project and this says team.
PROJECT_HEADER = "X-Team-Id"

def api_key() -> str:
    """The Hookdeck API key, namespaced name first, CLI convention second."""
    return os.getenv(API_KEY_ENV) or os.getenv(CLI_API_KEY_ENV, "")

# Suffixes appended to the configured prefix. Hookdeck documents these as
# X-Hookdeck-Signature, X-Hookdeck-EventID, X-Hookdeck-Attempt-Count, etc.
SIGNATURE = "signature"
SIGNATURE_2 = "signature-2"
EVENT_ID = "eventid"
# Hookdeck sends this too, and it is deliberately not used as a delivery
# identity. One request fans out to one event per matching connection, so two
# routes sharing a source produce two events carrying the same request id —
# dedup keyed on it would drop the second as a duplicate. `eventid` is the only
# per-delivery identifier, which is why its absence is treated as "no id" and
# said out loud rather than papered over.
REQUEST_ID = "requestid"
ATTEMPT_COUNT = "attempt-count"
ATTEMPT_TRIGGER = "attempt-trigger"
# Attempt triggers Hookdeck sends. INITIAL and AUTOMATIC are its own schedule;
# MANUAL and BULK_RETRY mean a person (or the agent, or the dashboard) asked
# for this delivery, which is a deliberate decision the adapter should honour
# even for an event whose automatic budget is spent. UNPAUSE is just a held
# event finally flowing, so it follows the normal rules.
OPERATOR_TRIGGERS = frozenset({"MANUAL", "BULK_RETRY"})
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
# this the saturation is not transient, and a fixed short interval would spend
# the automatic-retry budget in minutes instead of days.
#
# Kept well below the provisioned retry count (DEFAULT_RETRY_COUNT) so the
# exponential fallback has attempts left to spread. Equal values would mean the
# bare 503 first appearing on the final attempt, with no budget to save.
DEFAULT_DEFER_ATTEMPT_LIMIT = 2
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
# redelivery of THAT EXACT EVENT could still succeed.
#
# One declaration, two consumers: the connection's retry rule is derived from
# it, and `assert_declared_status` refuses any status not listed. Keeping them
# together is the point — a hand-written retry rule and the code that emits
# statuses drift apart quietly, and the symptom is a recoverable event that is
# simply never retried, with nothing recording that a choice was made.
#
# The test is "can a retry of this stored request ever succeed", answered
# against *this adapter's* operator surface rather than the status code in the
# abstract: 413 is retryable here only because max_body_bytes is operator
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
