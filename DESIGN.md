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

### Decode strictly, and check what your runtime actually does

RFC 8259 §8.1 requires UTF-8 for JSON exchanged between systems, so a body that
is not valid UTF-8 is malformed by definition. Reject it at ingress with a 400.

Do not assume your JSON parser does this for you — **every runtime checked so
far gets it wrong in a different direction**, and the failure is silent in the
worst cases:

| Runtime | Behaviour |
|---|---|
| Node | `Buffer.toString("utf8")` never throws; it substitutes U+FFFD, so invalid bytes *inside* a string value parse successfully into corrupted text |
| Python | raises on invalid bytes, but decodes with `surrogatepass`, so a CESU-8 lone surrogate parses and renders fine and then raises `UnicodeEncodeError` at the network boundary inside the run; also accepts UTF-16/32 by BOM sniffing |

Both silent paths end with mangled or unencodable text in a prompt, long after
the ack, in a layer that cannot explain itself. The fix is the same everywhere:
decode the raw bytes as strict UTF-8 *before* parsing (or re-encode and compare
against the raw body) and reject on failure. Verify your own runtime with a
lone-surrogate and a UTF-16 fixture rather than trusting this table.

Note the payload is still *authentic* in these cases — the signature is over
the raw bytes and it verified. What the agent would see simply is not what the
sender wrote.

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

Two properties of the identity worth knowing. `Idempotency-Key` equals the event
id and is stable across every retry attempt, so it is an equally valid key.
A *replay*, though, creates a **new** event with a new id — so replayed traffic
will not dedupe against the original, which is usually what you want, since a
replay is a deliberate request to run it again. If you need cross-replay dedup,
key on `x-hookdeck-requestid` or a provider-native event id instead.

Note also that Hookdeck's signature covers no timestamp, so there is no built-in
replay protection. Dedup is mandatory, not an optimisation.

State per event id: `attempt`, `run_count`, `status`, `updated_at`. Statuses:
`running`, `succeeded`, `failed`, `exhausted`. Prune terminal rows on a TTL;
**never prune `running`** — those rows are the input to boot-time recovery
(§5), so pruning one silently drops the work it represents.

## 4. Admission control

Cap concurrent runs with `max_concurrent`. Over the limit, respond **503 with
`Retry-After`** — not 429, not 200, not a local queue.

**Admission control and pause are not the same tool, and must not be reached
for interchangeably.** Admission control is per-event and automatic: Hookdeck
holds each deferred event and spreads the redelivery itself. Pause is an
operator action for a planned restart or a diagnosed outage. Pausing under
transient load looks like backpressure and is not — it defers the whole problem,
and unpause then delivers the accumulated backlog in one burst into the same
overload that caused it. Nothing in the request pipeline should pause
automatically; a pause tool exposed to an agent wants a bounded auto-resume for
the same reason.

Two rules follow, and both have bitten implementations that got them wrong:

1. Nothing is recorded in the ledger for a deferred event. Recording it would
   make Hookdeck's redelivery look like a duplicate, and the event would vanish.
2. The connection's retry rule must include `500-599` and `429`, or admission
   control turns into silent data loss.

Where the host supports it, the same limit should also be pushed into Hookdeck
as a destination-level `rate_limit` with `rate_limit_period: concurrent`.

Two limits on that, both verified against the OpenAPI spec:

- `concurrent` is valid on HTTP and Mock API destinations only.
  `DeliveryGroupRateLimitPeriod` is `second|minute|hour`, so `delivery_groups`
  can throttle per subject **by rate but not by concurrency**. Per-customer
  serialisation is not expressible; sending `concurrent` inside
  `delivery_groups` is rejected.
- CLI destinations have no `rate_limit` field at all. In CLI transport there is
  nothing to push down, and the host-side `max_concurrent` is the only
  admission control there is.

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

### Why `async_retry` works at all

A successful event can be retried manually. Confirmed with Hookdeck, and then
observed directly in the event log of a live project — every attempt below
returned 202, so Hookdeck had recorded the event as delivered, and it still
accepted two subsequent `MANUAL` retries:

```
evt_jyjuqko…  SUCCESSFUL  attempts=3  [(202,INITIAL), (202,MANUAL), (202,MANUAL)]
```

That one fact is what the whole contract rests on, because it makes the 202 ack
recoverable in **both** ways it can go wrong:

1. **The run fails.** Call `POST /events/{id}/retry`.
2. **The host dies mid-run.** At boot, every ledger row still marked `running`
   is an orphan — the process that owned it is gone, and Hookdeck already
   recorded that delivery as successful, so nothing will bring it back on its
   own. Same call, driven from the ledger.

Without it, an early ack would mean durably recording the rendered prompt
before acking and replaying from that on boot — i.e. every plugin owning a
local work queue. With it, Hookdeck *is* the work queue and no host needs one.
That matters most where the host's own durable queue is out of reach: OpenClaw's
`openChannelIngressQueue` is gated to bundled plugins.

Path 2 is why §3 says never prune `running` rows. That rule is load-bearing,
not tidiness: pruning one silently drops the work it represents.

## 6. Connection provisioning

Setup is an upsert keyed on the route name (`PUT /connections`), so it is safe
to re-run. Rules attached by default:

- `retry`: `strategy: exponential`, `response_status_codes: ["500-599"]`.
  Narrowing from Hookdeck's default of "any non-2xx" is deliberate — a 401 from
  a secret mismatch or a 404 from a missing route cannot be fixed by retrying,
  and the default would spend 50 attempts finding that out. Assert at
  provision time *and* in `doctor` that the rule covers every status the host
  actually emits; a rule that drifts narrower than the emitted set is silent
  data loss nothing else would surface.
- `deduplicate`: a short window, so a double-firing provider costs one run
- `filter`: **only** when the event name's location is known — a header for
  GitHub/GitLab/Shopify, or an explicitly configured body path. Guessing
  discards traffic silently, so when in doubt, filter host-side instead.

Destination auth is always `HOOKDECK_SIGNATURE`, in CLI and HTTP modes alike —
and `auth_type` must be accompanied by an `auth` object, even though
HOOKDECK_SIGNATURE's is empty and the OpenAPI schema does not mark it required.
Omitting it is rejected with `destination.config.auth is required`. Send
`{"auth_type": "HOOKDECK_SIGNATURE", "auth": {}}`.

Source verification is `config.auth_type` + `config.auth`. The docs page at
hookdeck.com/docs/sources still shows a legacy top-level
`verification: {type, configs}` object — that shape is stale and is not in the
current spec. Prefer a thin REST client generated against
`https://api.hookdeck.com/2025-07-01/openapi` over `@hookdeck/sdk`, which is at
0.4.0 with an archived repo and lags the current API.

**Every recovery path is capped by retention** — 3 days on Developer, 7 on Team,
30 on Growth. Say so in the README; an outage longer than the plan's retention
is not replayable by any of this.

## 7. Operator surface

Same five verbs, whatever the host calls its CLI:

| Verb | Does |
|---|---|
| `setup` | upsert the connection(s) for configured routes; `--dry-run` prints the payload |
| `status` | queue depth, failed events, open issues, local ledger state |
| `pause` / `resume` | `PUT /connections/{id}/pause` / `unpause` — zero-loss restarts. **Never `disable` or delete instead: both cancel pending events irrecoverably.** For a CLI transport, pause *before* stopping the listener — a CLI destination with nothing attached discards events rather than queueing them |
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

It bites hardest in a host that renders payloads into prompts for a model with
tools — a webhook body is then an injection surface. Three rules follow:

- Interpolate payload values as quoted data blocks, not as bare prose.
- **A webhook-triggered route must not send anything outbound by default.** An
  injected payload should not be able to make the agent message someone. The
  host's own inert default is fine where it has one (Hermes routes default to
  `deliver: log`); otherwise default the outbound channel off and make enabling
  it explicit per route.
- Keep injection-shaped fixtures in the template tests.

## 10. Optional extensions

Both default **off**, so default behaviour stays identical across the three
plugins. Adopted from the OpenClaw session.

**`Retry-After: -1` on permanently-invalid input.** Hookdeck honours a handler's
`Retry-After` over the connection's retry rules, and reads `-1` as "cancel all
further automatic retries" — turning 50 doomed attempts into one. The failure
mode is severe and silent: one over-strict validator discards live traffic,
unrecoverable once retention lapses. So: never cancel anything a config change
could fix, record every cancellation where `status` will surface it, and ship it
off by default. Measure how often it *would* have fired before enabling.

Gate the reasons behind an explicit allowlist once there is more than one of
them, so "which code paths can discard production traffic" is answerable by
reading one file rather than grepping. With a single trigger the allowlist is
ceremony and a plain flag is clearer — this is a difference in how much surface
each host has to police, not a difference in the rule.

**Last-attempt detection.** An absent or empty `x-hookdeck-will-retry-after`
means this is the final automatic attempt — the cleanest dead-letter trigger
available. No downside; surface it in logs and in whatever the host uses for
run metadata.

## 11. Check the host's own authorization layer

A signature-verified webhook has no user in the messaging sense, and a host
that gates inbound on a user allowlist will refuse every delivery — for Hermes,
`Unauthorized user: hookdeck:<route>`, logged as a warning with the event
otherwise looking perfectly delivered.

Hosts usually exempt their *own* webhook surface from this and forget that a
plugin platform is not covered by the exemption. Hermes hardcodes
`if source.platform in {HOMEASSISTANT, WEBHOOK}: return True` on the grounds
that HMAC verification in the adapter is the authorization — true of any
Hookdeck plugin too, but the test is on the enum member, so a plugin registered
under its own name falls through to default-deny.

Find the host's sanctioned mechanism rather than reaching for an allow-all
switch (Hermes exposes `authorization_is_upstream`, whose contract is exactly
"authorized by a trusted upstream over an authenticated transport"). And gate
it on verification actually being on, so a local-testing escape hatch does not
also switch off the host's allowlist.

This only shows up when running a real host process. It is invisible to unit
tests, and invisible to an ingress-only live test, because the request
succeeds — it is the dispatch after it that is dropped.

## 12. What each host may legitimately differ on

- **Transport.** CLI tunnel, HTTP push, or whatever the host natively offers.
- **Storage.** Any durable store that can express the section 3 rule.
- **Route vocabulary.** Reuse the host's existing concepts rather than importing
  another plugin's.
- **Ack timing granularity.** A host with no completion hook cannot implement
  `sync`; say so instead of faking it.
