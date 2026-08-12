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

Pausing also covers a second, less obvious loss. A run already in flight when
you stop the gateway is interrupted, and Hermes reports an interrupted run to
the plugin as a *success* — so the adapter records `succeeded` and boot
recovery, which only looks for rows left `running`, finds nothing to bring
back. A gateway that is killed outright recovers; one stopped politely
mid-run does not. Pausing first means there is no delivery in flight to
interrupt. See [limitations](limitations.md).

**The gateway keeps its own CLI session.** Two independent things decide "which
project": your API key decides what `setup`, `status` and the retry hand-back
act on, while the Hookdeck CLI's own config decides what `hookdeck listen`
forwards from. Nothing reconciles them, and when they differ every visible
signal says the gateway is fine — `setup` succeeds, the adapter logs that it is
listening, and only the tunnel's restart loop (`no connection found matching
filter`) says otherwise, while every event becomes a `CLI_DISCONNECTED` ignored
event.

So the adapter authenticates a CLI config of its own from the API key it
already has, at `~/.hermes/hookdeck/cli-config.toml`, and passes
`--hookdeck-config` to every CLI call. Two projects cannot drift apart when
only one of them is configurable, and `hermes hookdeck doctor` compares them.

This is deliberately not `hookdeck ci` against your shared config: that
rewrites `~/.config/hookdeck/config.toml` and switches the CLI's *active
project*, so anyone using the CLI for other work would find their environment
repointed by starting a gateway — and it does so even with `--local`, which
claims to write only to the current directory
([hookdeck-cli#332](https://github.com/hookdeck/hookdeck-cli/issues/332)).

Set `cli_config_path: ""` to use your own `hookdeck login` session instead, and
accept that the two projects can then diverge.

Use a CLI version of at least 2.3.2. Earlier ones stop delivering after a
listen session expires without saying so, which from the gateway's side looks
identical to "no events are arriving". `hermes hookdeck doctor` checks the
version *and* prints which binary it resolved — an npm global shadowing a
Homebrew install is common, and version-checking one binary while launching
another is worse than not checking. Set `cli_binary` to pin it explicitly.

Two behaviours worth recognising in the Hookdeck event log when the local
server is down. With no listen session attached at all, attempts record
`CLI_UNAVAILABLE` and no response status. With a session attached but the local
port refusing, the CLI reports a **503** upstream (observed on CLI 2.4.0) —
which is one reason the provisioned retry rule covers `500-599`: a gateway that
has not finished starting produces exactly this, and those events must come
back.

A restarting gateway can record both signatures on the *same* event, since the
listen session drops and returns while retries are in flight. That pattern —
`CLI_UNAVAILABLE` and `503` interleaved across attempts of one event — is a
bounce, not a capacity problem, and is the discriminator the bundled triage
skill tells the agent to check before blaming a concurrency limit.

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
