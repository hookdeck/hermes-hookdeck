---
name: triage-webhook-failures
description: Investigate and clear failed webhook events in the Hookdeck queue. Use when events failed while the gateway was down, when a route's runs are erroring, or when asked to check whether anything was missed.
---

# Triage webhook failures

Failed events sit in Hookdeck until someone decides what they are. This is how
to decide.

## 1. Establish the scale

Call `hookdeck_queue_status`. A non-zero pending depth with no failures usually
means delivery is throttled or the connection is paused — not that anything is
broken.

## 2. Read the failures, do not guess

Call `hookdeck_list_failed_events`. Group what comes back by `error_code` and
`response_status` before touching anything. The grouping is the diagnosis:

- **`503` / `ERR_CONNECTION` in a burst** — the gateway hit its concurrency
  limit or was down. The events are fine; retrying is the whole fix.
- **`401`** — a signing-secret mismatch. Retrying changes nothing until
  `HOOKDECK_WEBHOOK_SECRET` matches the project's signing secret. Say so
  instead of retrying.
- **`404`** — no route matched the source. Fix the route config first; the
  events will keep failing otherwise.
- **`500` on a handful of events while others on the same route succeeded** —
  the payloads differ. Read one with `hookdeck_get_event_body` before deciding.

## 3. Retry only what will actually succeed

Use `hookdeck_retry_event` per event when the causes differ. Use
`hookdeck_bulk_retry` only when every failure in scope shares one cause that is
now fixed — scope it with `since` and `connection_id` rather than retrying the
whole history.

Retrying an event re-runs the agent, which costs tokens and can repeat side
effects. An event whose work already happened by another route should be left
alone and reported, not retried.

## 4. Report

State what failed, why, what you retried, and what you deliberately did not.
Anything you could not diagnose from the payload and error code needs a human —
name it explicitly rather than retrying hopefully.

## Pausing

`hookdeck_pause_connection` holds events in Hookdeck instead of dropping them.
Reach for it before a restart, or when you have established that runs are
failing for a reason you cannot fix yet — queueing is better than a run of
failures. Always resume afterwards with `hookdeck_resume_connection`.
