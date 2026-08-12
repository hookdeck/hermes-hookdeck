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

- **`CLI_UNAVAILABLE` with no `response_status`** — in `cli` mode, no listen
  session was attached: the gateway was not running, or its tunnel was down.
  The events are fine; retrying once the gateway is up is the whole fix.
- **`503` with no `error_code`** — a listen session *was* attached but the
  local port refused the connection. Usually a gateway that had not finished
  starting, had just crashed, or was bound to a different port. Also fine to
  retry once it is up.

  Do not read `503` as "the gateway hit its concurrency limit". That is a real
  cause but a much rarer one, and nothing in the event distinguishes the two —
  so check the cheap thing first: is the gateway up and listening on the port
  the route expects? Both codes can appear on the *same* event when a gateway
  is restarting, which is itself the signature of a bounce rather than a
  capacity problem.
- **`401`** — a signing-secret mismatch. Retrying changes nothing until
  `HOOKDECK_EG_WEBHOOK_SECRET` matches the project's signing secret. Say so
  instead of retrying.
- **`404`** — no route matched the source. Fix the route config first; the
  events will keep failing otherwise.
- **`500` on a handful of events while others on the same route succeeded** —
  the payloads differ. Read one with `hookdeck_get_event_body` before deciding.

A status code narrows the cause; it rarely settles it. Where two causes fit
what you can see, say which one you checked and which you could not, and give
the reader the discriminator rather than picking the more interesting story.
A wrong root cause stated confidently is worse than "these two both fit" —
it sends someone tuning a limit that was never the problem.

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

## Retry, not replay

Everything here uses Hookdeck's **retry**: a fresh delivery attempt for the
same event. Hookdeck also has **replay**, which re-ingests the original request
and creates *new* events with the connection's current rules applied — the
right tool after fixing a filter or transformation, and not something these
tools do. If retrying is not working because the connection itself is
misconfigured, say so rather than retrying repeatedly.

## Pausing

`hookdeck_pause_connection` holds events in Hookdeck instead of dropping them.
Reach for it before a restart, or when you have established that runs are
failing for a reason you cannot fix yet — queueing is better than a run of
failures. Always resume afterwards with `hookdeck_resume_connection`.
