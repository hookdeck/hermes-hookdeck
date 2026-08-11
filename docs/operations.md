# Running it

Two transports, and the operational cautions that come with each. The
[README](../README.md) has the short version; this is what you need before
depending on it.

## CLI mode (no public URL)

The default. The Hookdeck CLI holds an outbound connection and forwards events
to a loopback listener, so a laptop or a homelab box behind NAT works without
ngrok or a VPS.

**A CLI destination is not a durable buffer.** With no listener attached,
events become `CLI_DISCONNECTED` ignored events and the request is discarded —
not queued, not retried. An *abnormal* disconnect gets a short server-side
grace window in which events are still created and fail as `CLI_UNAVAILABLE`,
which keeps them in the normal retry pipeline; a clean Ctrl+C forfeits even
that. So on a planned shutdown, **pause the connection before you stop the
gateway**:

```bash
hermes hookdeck pause github-prs
```

That is the durable path — paused events are held at `HOLD` and delivered on
resume. Never reach for `disable` instead: it cancels pending events
irrecoverably, as does deleting the connection.

The adapter does **not** run `hookdeck ci` to authenticate the CLI. That command
looks like a harmless idempotent login and is not: it rewrites the shared config
at `~/.config/hookdeck/config.toml`, swapping the stored key for a CLI session
key and switching the CLI's *active project*. Anyone using the CLI for other
work would find their environment repointed by starting a gateway. Log in
yourself with `hookdeck login`; set `cli_login: true` only if you accept that.

Use a CLI version of at least 2.3.2. Earlier ones stop delivering after a
listen session expires without saying so, which from the gateway's side looks
identical to "no events are arriving". `hermes hookdeck doctor` checks the
version *and* prints which binary it resolved — an npm global shadowing a
Homebrew install is common, and version-checking one binary while launching
another is worse than not checking. Set `cli_binary` to pin it explicitly.

Two behaviours worth recognising in the Hookdeck event log when the local
server is down. With no listen session attached at all, attempts record
`CLI_UNAVAILABLE` and no response status. With a session attached but the local
port refusing, the CLI reports a **500** upstream — which is one reason the
provisioned retry rule covers `500-599`: a gateway that has not finished
starting produces exactly this, and those events must come back.

Install the [Hookdeck CLI](https://hookdeck.com/docs/cli), add a route to
`~/.hermes/config.yaml` (see [`examples/config.yaml`](../examples/config.yaml)),
then:

```bash
hermes hookdeck setup github-prs --source github --source-type GITHUB
```

That creates a source with GitHub's verification already configured, a CLI
destination, and a connection carrying exponential retries and a dedup window.

Start the gateway and the adapter launches `hookdeck listen` for you — one
process per route, since the CLI forwards a single source each, given
`--path /hookdeck/<route>` so the adapter resolves the route from the path.
Every route therefore needs a `source`, or a shared
`platforms.hookdeck.extra.source`.

Point GitHub at the source URL Hookdeck gives you and open a pull request.

`hookdeck listen` creates the source itself if it does not exist, so it will
work without `setup` — but you get a bare connection with none of the retry,
dedup or filter rules, which is most of the point.

## Push mode

For a gateway with a reachable URL. Push mode unlocks the settings CLI
destinations do not support: delivery rate limits, delivery groups, issue
triggers and alerting.

Set `mode: push` and `public_url` in the config, then:

```bash
hermes hookdeck setup --all --mode push --rate-limit 2 --rate-limit-period concurrent
```


## Operator commands

```bash
hermes hookdeck setup <route> [--all] [--dry-run]   # create/update connections
hermes hookdeck status                              # queue depth, failures, issues
hermes hookdeck pause <connection>                  # hold events server-side
hermes hookdeck resume <connection>                 # drain them
hermes hookdeck retry <event_id> | --failed         # re-attempt delivery
hermes hookdeck doctor                              # check the whole setup
```
