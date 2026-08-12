# hermes-hookdeck

**A durable, verified queue in front of your Hermes agent, so a webhook can trigger an agent run without the usual ways that goes wrong.**

Agent runs are not ordinary webhook handlers. They take seconds to minutes, cost money per execution, and must not run twice for the same event. Hermes's built-in webhook platform is fine for trying things out, but in production it drops bursts over 30/min and forgets duplicates after a restart. Any run that fails after the 202 is sent is simply lost. This plugin replaces that ingestion path with the [Hookdeck Event Gateway](https://hookdeck.com/docs), plus a local ledger that tracks the outcomes Hookdeck can't see.

> Inbound only, for third-party events arriving at your agent. It is not [Outpost](https://hookdeck.com/docs/outpost) and nothing here helps Hermes publish webhooks.

## Why

| | Built-in webhooks | With this plugin |
|---|---|---|
| Signature verification | Limited providers | ~140 provider schemes verified by Hookdeck |
| Gateway offline | Events lost | Paused events held server-side, drained on resume |
| Traffic bursts | 30/min fixed window, excess dropped | Queued; overflow answered with 503 + `Retry-After` |
| Duplicates | In-memory 1h cache | Hookdeck dedup + restart-safe SQLite ledger |
| Failed runs | Lost after the 202 | Redelivered by Hookdeck |
| Mid-run crashes | Silently lost | Boot-time recovery via the ledger |

The short version: with the built-in platform, a webhook provider believes an event was delivered the moment Hermes returns 202 — whatever happens to the agent run afterwards. This plugin keeps a run ledger in SQLite (`~/.hermes/hookdeck/state.db`) so failed and interrupted runs are redelivered instead of vanishing.

## Install

```bash
# via the Hermes plugin manager
hermes plugins install hookdeck/hermes-hookdeck
hermes plugins enable hookdeck

# or via pip
pip install hermes-hookdeck && hermes plugins enable hookdeck

# or from source
git clone https://github.com/hookdeck/hermes-hookdeck ~/.hermes/plugins/hermes-hookdeck
```

Configure two environment variables from your Hookdeck dashboard (Project Settings > Secrets). They are prefixed `HOOKDECK_EG_` for the Event Gateway, since Hookdeck's platform is more than one product:

```bash
export HOOKDECK_EG_API_KEY=...        # provisions connections
export HOOKDECK_EG_WEBHOOK_SECRET=... # verifies deliveries
```

Then create a route and check the setup:

```bash
hermes hookdeck setup my-route
hermes hookdeck doctor
```

A [free Hookdeck account](https://hookdeck.com/signup) is enough for development and small production workloads.

## How it works

Events flow: **provider -> Hookdeck -> (CLI or HTTP push) -> plugin listener -> Hermes agent run**, with three reliability layers on top:

1. **Signature verification.** Every delivery carries an `x-hookdeck-signature` header, verified with HMAC-SHA256 in constant time. Provider-side verification (Stripe, Shopify, GitHub, and ~140 others) happens at Hookdeck's edge before the event ever reaches you.
2. **Run ledger.** A local SQLite database records each delivery attempt and its agent-run outcome. If the process crashes mid-run, boot-time recovery finds the orphaned events and re-runs them.
3. **Backpressure.** `max_concurrent` caps simultaneous agent runs. Requests over the cap get a 503 with `Retry-After`, and Hookdeck redelivers on schedule instead of piling runs onto your box.

### Acknowledgment modes

- **`async_retry`** (default): respond 202 immediately, run the agent async, call Hookdeck's retry API if the run fails.
- **`sync`**: hold the HTTP response until the agent finishes, letting Hookdeck's native retry rules apply. Best for short runs.

### Connection modes

- **CLI mode** (default): the Hookdeck CLI holds an outbound connection and forwards events to a loopback listener. Works behind NAT with no public URL, ngrok, or VPS. Pause the connection before shutdown to avoid losing events while disconnected.
- **Push mode**: for publicly reachable gateways. Unlocks delivery rate limits, delivery groups, issue triggers, and alerting.

## Operator commands

```bash
hermes hookdeck setup <route>       # create/update connections
hermes hookdeck status              # queue depth, failures, issues
hermes hookdeck pause <connection>  # hold events server-side
hermes hookdeck resume <connection> # drain them
hermes hookdeck retry <event_id>    # re-attempt a delivery
hermes hookdeck doctor              # check the whole setup
```

## Agent tools

The plugin exposes the queue to the agent itself:

- `hookdeck_queue_status`
- `hookdeck_list_failed_events`
- `hookdeck_get_event_body`
- `hookdeck_retry_event`
- `hookdeck_bulk_retry`
- `hookdeck_pause_connection` / `hookdeck_resume_connection`

A bundled `triage-webhook-failures` skill teaches the agent to group failures by error code, retry what a retry will actually fix, and report the rest instead of retrying hopefully.

## Dashboard

`hermes dashboard` gains a Hookdeck tab: queue depth, failed deliveries with one-click retry, agent-run outcomes from the ledger, and per-connection pause/resume. Optional, no build step.

## Documentation

- [How it fits together](docs/architecture.md) — where each piece runs, the
  delivery pipeline, and the three different things called "CLI"
- [How the reliability works](docs/reliability.md) — verification, the run
  ledger and its idempotency rule, backpressure, ack modes, and why retry
  rather than replay
- [Running it](docs/operations.md) — CLI and push mode in full, the operational
  cautions that matter in production, and the operator commands
- [Trust boundary](docs/security.md) — payload text is third-party input that
  reaches a prompt; what to do about it
- [Limitations](docs/limitations.md) — the complete set, including the ones
  that only surface once you provision connections yourself
- [Development](docs/development.md) — running the tests, and what lives where
- [`examples/config.yaml`](examples/config.yaml) — every setting, annotated

## Limitations

- CLI destinations don't support delivery rate limits or issue triggers; use push mode for those.
- CLI mode runs one `hookdeck listen` process per route; impractical beyond a handful of routes.
- JSON and form-encoded bodies only. XML and plain-text providers are rejected.
- Recovery is bounded by your Hookdeck plan's retention window (3-30 days by tier).
- Boot-time recovery is at-least-once: an event whose run completed just before a crash may run again. Keep agent actions idempotent where you can.

## Releasing

Publishing a GitHub release is what ships the package — there is no separate
approval step, and pushing a bare tag does nothing.

1. **Get everything onto `main`** and make sure its checks are green.
2. **Bump `__version__`** in [`hookdeck/__init__.py`](hookdeck/__init__.py) and
   merge that to `main`. The release tag must equal `v` + this value; the
   workflow checks it before building and fails the release otherwise.
3. **Publish a GitHub release** — *Releases → Draft a new release*, tag
   `vMAJOR.MINOR.PATCH`, target `main`, write the notes, Publish. Tick
   *Set as a pre-release* for `rc` versions.

[`release.yml`](.github/workflows/release.yml) then runs the test suite against
the release's own commit, builds, publishes to PyPI via Trusted Publishing (no
stored token), and attaches the wheel and sdist to the release.

**PyPI is append-only** — a published version number can never be reused, so
get it right before publishing. Anything under `hookdeck/` ships inside the
wheel, including the bundled skill; anything outside it does not.

Asking an agent to "create a new release" runs
[`skills/hermes-hookdeck-release`](skills/hermes-hookdeck-release/SKILL.md),
which covers the same ground with the SemVer rules and the gates to check
first.

## License

MIT
