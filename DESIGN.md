# Webhook reliability contract

This is the shared design for the Hookdeck plugins across Hermes Agent,
OpenClaw and n8n. Three hosts, three languages, one set of semantics — so that
"what happens when the run fails" has the same answer everywhere, and someone
who learns one plugin already knows the others.

Divergence should be forced by the host, not chosen by the author. Where a host
genuinely cannot support something here, say so in its README under
Limitations rather than inventing a different rule.

## 1. The problem this solves

A webhook sender expects a fast, idempotent, retryable receiver. An agent run is
none of those things: it takes seconds to minutes, it costs money each time, and
running it twice can repeat side effects. Every host in scope resolves that
mismatch the same way — acknowledge quickly, then process — which quietly means
the acknowledgement is a lie: it says "handled" when it means "received".

Hookdeck makes the acknowledgement honest, because the event still exists after
the ack and can be redelivered.

## 2. Verification

Verify `x-hookdeck-signature`, computed as
`base64(HMAC-SHA256(raw_body, signing_secret))`, in constant time, before
parsing or logging the payload.

- Also accept `x-hookdeck-signature-2`; it carries the previous secret during a
  roll, and rejecting it drops live traffic mid-rotation.
- The `x-hookdeck` prefix is configurable per project. Read it from config,
  do not hardcode the literal.
- An absent or empty secret is a failure, never a bypass.
- A local-testing escape hatch is allowed only when bound to loopback, and must
  be named so it cannot be mistaken for a setting (`INSECURE_NO_AUTH`).

Provider-specific verification stays at the edge. A plugin that reimplements
Stripe's or Shopify's scheme has misunderstood the integration.

## 3. Identity and deduplication

`x-hookdeck-eventid` is the identity of a delivery. `x-hookdeck-attempt-count`
is which try it is.

Deduplication must be **persistent** — a process restart cannot be allowed to
re-run work — and must not block legitimate retries. The rule:

> Admit a delivery when its attempt number is greater than the highest attempt
> already recorded for that event id. Otherwise reject it as a duplicate.

When the attempt header is absent, admit only if the previous run for that event
is recorded as failed. Storage is the host's business (SQLite, the host's own
store, a workflow static data slot); the rule is not.

State per event id: `attempt`, `run_count`, `status`, `updated_at`. Statuses:
`running`, `succeeded`, `failed`, `exhausted`. Prune terminal rows on a TTL;
never prune `running`.

## 4. Admission control

Cap concurrent runs with `max_concurrent`. Over the limit, respond **503 with
`Retry-After`** — not 429, not 200, not a local queue.

Two rules follow, and both have bitten implementations that got them wrong:

1. Nothing is recorded in the ledger for a deferred event. Recording it would
   make Hookdeck's redelivery look like a duplicate, and the event would vanish.
2. The connection's retry rule must include `500-599` and `429`, or admission
   control turns into silent data loss.

Where the host supports it, the same limit should also be pushed into Hookdeck
(`rate_limit` with `rate_limit_period: concurrent`), and `delivery_groups` used
to serialise runs per subject key.

## 5. Acknowledgement modes

Exactly two, named identically across plugins:

- **`async_retry`** (default) — ack 202 once admitted, run in the background,
  and on failure call `POST /events/{id}/retry`. Retry state lives in Hookdeck,
  so it survives a host restart. Stop after `max_agent_retries` and mark the
  event exhausted rather than looping.
- **`sync`** — hold the response until the run finishes, bounded by
  `sync_timeout_seconds`; 2xx on success, 5xx on failure, so Hookdeck's own
  retry rules apply. On timeout, degrade to 202 — answering 5xx would redeliver
  work that is still running.

"Backpressure" is not a mode. `max_concurrent` applies in both.

## 6. Connection provisioning

Setup is an upsert keyed on the route name (`PUT /connections`), so it is safe
to re-run. Rules attached by default:

- `retry`: `strategy: exponential`, `response_status_codes: ["500-599", "429"]`
- `deduplicate`: a short window, so a double-firing provider costs one run
- `filter`: **only** when the event name's location is known — a header for
  GitHub/GitLab/Shopify, or an explicitly configured body path. Guessing
  discards traffic silently, so when in doubt, filter host-side instead.

Destination auth is always `HOOKDECK_SIGNATURE`, in CLI and HTTP modes alike.

## 7. Operator surface

Same five verbs, whatever the host calls its CLI:

| Verb | Does |
|---|---|
| `setup` | upsert the connection(s) for configured routes; `--dry-run` prints the payload |
| `status` | queue depth, failed events, open issues, local ledger state |
| `pause` / `resume` | `PUT /connections/{id}/pause` / `unpause` — zero-loss restarts |
| `replay` | `POST /events/{id}/retry`, or bulk retry scoped by time and connection |
| `doctor` | check credentials, transport, config and stale local state, and say what is wrong |

## 8. Agent-facing tools

Where the host has a tool/skill concept, expose the queue to the agent:
queue status, list failed events, fetch an event body, retry one, bulk retry,
pause and resume. Bulk retry must be scopable; an unscoped retry-everything is a
footgun that costs real money.

## 9. Trust boundary

A valid signature authenticates the sender, not the content. Payload text is
third-party input that ends up in a prompt.

Every plugin states this in its README, and where the host supports a system
hint, sets one telling the model that payload text is data and never an
instruction addressed to it. Recommend sandboxed execution, scoped tools and
approval gates on webhook-triggered paths.

## 10. What each host may legitimately differ on

- **Transport.** CLI tunnel, HTTP push, or whatever the host natively offers.
- **Storage.** Any durable store that can express the section 3 rule.
- **Route vocabulary.** Reuse the host's existing concepts rather than importing
  another plugin's.
- **Ack timing granularity.** A host with no completion hook cannot implement
  `sync`; say so instead of faking it.
