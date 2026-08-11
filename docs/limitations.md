# Limitations

The README lists the ones most people hit. This is the complete set,
including those that only surface once you provision connections yourself.

- CLI destinations do not support delivery rate limits or issue triggers — a
  Hookdeck restriction, not a plugin one. `max_concurrent` still applies, since
  it is enforced adapter-side. Use push mode if you need gateway-side throttling
  or alerting.
- CLI mode runs one `hookdeck listen` process per route. That is fine for a
  handful; a gateway with dozens of routes wants push mode.
- `setup` only pushes an `events` filter down into Hookdeck when it knows where
  the event name lives: a header for GitHub, GitLab and Shopify, or a body path
  you set with `event_path`. Otherwise the filter stays adapter-side, because a
  wrong filter discards traffic silently.
- Named source types (`STRIPE`, `SHOPIFY`, …) still need the provider's own
  signing secret entered on the source in the Hookdeck dashboard. `setup`
  creates the source with the right verification shape but cannot invent the
  secret.
- Delivery is push-only, in both directions. Hookdeck pushes to the adapter and
  the adapter pushes retry requests back; there is no lease-and-ack loop, and no
  pull API to build one from — the Events API is for inspection, with no ack,
  lease or consumer group. So if you can neither run the CLI nor expose a URL,
  this plugin cannot help you. And "the event is safe in Hookdeck" holds because
  a delivered-but-failed event stays retryable, not because anything holds a
  lock on it — which is why boot recovery reconciles `running` ledger rows
  itself.
- In `cli` mode the gateway needs an API key, not just a signing secret: it is
  what pins the CLI session to the right project. Without one the adapter falls
  back to your ambient `hookdeck login`, and the project it forwards from can
  then differ from the one it manages. `doctor` reports that; it cannot fix it.
- Delivery groups throttle per subject by rate, not by concurrency, because
  Hookdeck's group-level period is `second|minute|hour`.
- Every recovery path is bounded by your plan's retention: 3 days on
  Developer, 7 on Team, 30 on Growth. An outage longer than that is not
  replayable.
- Only JSON and form-encoded bodies are understood. XML or plain-text
  providers are rejected with 400 and, since no operator change makes such a
  body parse, never retried. Put a Hookdeck transformation in front of the
  connection to convert them, or use a provider webhook that speaks JSON.
- Boot-time recovery re-runs an event whose run might in fact have completed
  in the instant before a crash. That is the at-least-once contract the whole
  design assumes; set `recover_on_boot: false` if it is wrong for your routes.

## Hookdeck can do more than this plugin asks it to

Everything above is what *cannot* be done. This is the other boundary: things
Hookdeck offers that the plugin does not wire up, so nobody mistakes the edge of
`hookdeck/api.py` for the edge of the product. Each is a candidate, not a
promise.

**Getting events in.** The [Publish API](https://hookdeck.com/docs/api/publish.md)
sends a request to any source, authenticated with the same API key. Nothing here
calls it, and two uses stand out: a `hermes hookdeck test <route>` that puts a
real event through the real connection without waiting for a provider, and a way
for Hermes to enqueue durable work for itself.

**Recovering events ignored while the CLI was disconnected.** The caveat above
says events arriving with no listener attached are discarded. That is what *this
plugin* does with them, not what Hookdeck can do:
[`POST /bulk/ignored-events/retry`](https://hookdeck.com/docs/api/bulk.md#bulk-retry-ignored-events)
takes a query filtered by `cause` and `webhook_id`, and `CLI_DISCONNECTED` is a
first-class cause. Retrying re-runs the *original request* through ingestion, so
the recovery is genuine rather than a status change.

One ordering rule makes it work, and it is the whole trick: **reconnect first,
then retry.** The retry re-evaluates the same "no attached listen session"
condition that ignored the event, so retrying while still disconnected simply
produces another ignored event.

**Bulk operations with the safety catch on.** `hookdeck_bulk_retry` is an
*agent-callable* tool that fires `POST /bulk/events/retry` immediately. Hookdeck
can estimate a bulk operation before running it (`GET /bulk/events/retry/plan`)
and cancel one in flight. An agent that could see "this would re-run 4,000
events" before committing is a materially safer agent.

**Requests, not just events.** A Hookdeck *request* is what the provider sent;
an *event* is one connection's copy. `/bulk/requests/retry` and
`/bulk/requests/replay` re-run the request, producing fresh events for every
matching connection — the right instrument after fixing a connection that was
misconfigured when the traffic arrived.

**Alerting, shaping and metrics.** Issue triggers and notifications can report a
failing connection without anyone watching `hermes hookdeck status`; `setup`
provisions none. Transformations run JavaScript before delivery — the documented
workaround for the JSON-only limitation above is to add one by hand, and `setup`
could manage it. The dashboard reads `GET /metrics/queue-depth`; request, event
and attempt metrics would turn that number into a trend.
