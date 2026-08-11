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
- The plugin does not poll. Hookdeck has no pull/consumer API — the Events API
  is inspection-only, with no ack, lease or consumer group — so if you cannot
  run the CLI and cannot expose a URL, it will not help you.
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
