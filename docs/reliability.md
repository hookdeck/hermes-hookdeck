# How the reliability works

Four mechanisms, and the reasoning behind each — including which parts are
Hookdeck's and which exist only because the adapter acknowledges early.

**One verifier.** Hookdeck verifies Stripe's signature, Shopify's HMAC,
Twilio's, and so on, then signs its own delivery with
`base64(HMAC-SHA256(body, secret))` in `x-hookdeck-signature`. The adapter
checks that one scheme, in constant time, before touching the payload.
`x-hookdeck-signature-2` is also accepted so a secret roll does not drop live
traffic.

**A run ledger, not a second queue.** The plugin provisions Hookdeck's
`deduplicate` rule and does not reimplement it — that rule suppresses duplicate
*requests* at ingestion, and Hookdeck's docs are explicit that it is best-effort
and destinations should be idempotent regardless.

What the local SQLite file at `~/.hermes/hookdeck/state.db` holds is the half
Hookdeck structurally cannot see. The adapter answers 202 the moment it admits
an event, so from the queue's side every delivery succeeded — whatever the
agent then did. A run that fails after the ack is invisible there: Hookdeck
cannot know, so it cannot retry, so something local has to remember and ask.
That memory drives the retry budget, boot recovery, and what `status` and the
dashboard show.

Idempotency falls out of the same records. Each delivery carries an attempt
number, and one is admitted only when its number exceeds the highest already
recorded — genuine repeats reuse a number, real retries increment it, which is
what lets retrying and not-double-running coexist.

In `sync` mode most of this is moot: there the response *is* the outcome and
Hookdeck drives the retries, leaving only boot recovery for runs that outlasted
the timeout.

**Backpressure that queues in Hookdeck, not here.** `max_concurrent` caps
agent runs in flight. An event over the limit gets a 503 and a `Retry-After`,
and that is the whole mechanism — nothing is written down, no local queue
exists, the event simply stays in Hookdeck and comes back. (Nothing is recorded
for a deferred event on purpose: a ledger entry would make the redelivery look
like a duplicate.)

The *count* is local because Hookdeck cannot take it. Its destination rate
limit with `rate_limit_period: concurrent` caps "simultaneous delivery attempts
open to the destination", which ends when the connection closes — and in
`async_retry` that is the 202, milliseconds in, while the run continues for
seconds or minutes. Set it to 1 and it would still never engage. Hookdeck can
limit deliveries; only the adapter can see runs. CLI destinations have no
`rate_limit` field at all, so in the default transport it is not on the table
either way.

In `sync` mode this inverts: the delivery stays open for the run's duration, so
a destination-level `--rate-limit N --rate-limit-period concurrent` genuinely
does cap concurrent runs, and does it better — Hookdeck holds the event without
a 503 round trip or a dent in its retry budget. Worth preferring there, with
`max_concurrent` left as the backstop for runs that outlast the sync timeout.

`--group-key` throttles per subject (`--group-rate 1 --group-period minute`).
Delivery groups accept only `second|minute|hour`, so per-subject *concurrency*
is not expressible — `concurrent` is destination-level only.

**Outcomes reported, not assumed.** `ack_mode` decides how:

- `async_retry` (default) — ack 202 immediately, run the agent in the
  background, and call `POST /events/{id}/retry` if the run fails. Retry state
  lives in Hookdeck, so it survives a gateway restart. Stops after
  `max_agent_retries` and marks the event exhausted rather than looping.

  The same call covers the harder case. If the gateway dies mid-run, Hookdeck
  has already recorded that delivery as successful and will never redeliver it
  on its own — so at startup the adapter reads every ledger row still marked
  `running`, which by then can only be an orphan, and asks for redelivery.
  Between the two, an early ack is recoverable in both directions, which is
  what lets Hookdeck be the work queue instead of the plugin owning one.
- `sync` — hold the HTTP response until the run finishes, bounded by
  `sync_timeout_seconds`, so the event's status in Hookdeck is the agent's real
  outcome and Hookdeck's own retry rules apply. A run that outlasts the timeout
  degrades to 202; answering 5xx there would redeliver work still in progress.

## Retry, and why not replay

`retry` is Hookdeck's retry — `POST /events/{id}/retry`, a fresh delivery
attempt for the same event — and so are the dashboard's Retry buttons and the
`hookdeck_retry_event` tool. None of it is reimplemented locally; the plugin
asks Hookdeck and Hookdeck redelivers.

Hookdeck's **replay** is a different thing: it re-ingests the original request
and creates *new* events, with the connection's rules re-evaluated against
current config. That is the right tool after fixing a filter or a
transformation, and the plugin deliberately does not wrap it — a replayed event
is new work, arrives with a new id, and needs no special handling here.

`pause` before an upgrade and `resume` afterwards is a zero-loss restart:
events accumulate in Hookdeck rather than hitting a dead port.
