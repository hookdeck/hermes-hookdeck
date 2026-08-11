# Changelog

Notable changes per release. Versions follow [semantic versioning](https://semver.org),
with the caveat that until 1.0 the config surface — `platforms.hookdeck.extra`
— may change in a minor release, and any such change is listed here.

## Unreleased

### Fixed

- A signature header containing a non-ASCII byte answered 500 instead of 401.
  The 500 skipped the `EMITTED_STATUS_RETRYABLE` guard and fell inside the
  provisioned retry rule's `500-599` range, so Hookdeck retried a forged
  request rather than dropping it.
- A delivery's event id no longer falls back to a bare `X-Request-ID` header.
  That header is not a Hookdeck identifier, is not subject to `header_prefix`,
  and is not unique per delivery — one request fans out to one event per
  matching connection. Deliveries genuinely missing `x-hookdeck-eventid` are
  processed with the existing warning that dedup and retry are unavailable.
- The marker tracking a `sync` run that outlasted its timeout is now cleared on
  every terminal path, not only on success. It previously survived an exhausted
  retry budget or an abandoned hand-back, and a later run of the same event id
  would be treated as having been acked early when it had not.

### Added

- Continuous integration: lint, a test matrix across Python 3.10–3.13, and a
  packaging job that asserts the built wheel still carries `plugin.yaml`, the
  dashboard bundle and the bundled skill — without which the plugin installs
  but registers nothing.
- A weekly check that the Hermes internals this plugin subclasses and calls
  still exist upstream, since the test suite otherwise runs entirely against
  `tests/hermes_stub.py` and cannot notice the real thing moving.
- Ruff, with `BLE001` selected so each deliberate blind `except` carries its
  reason at the point it is written.

### Changed

- The package version is read from `hookdeck.__version__` rather than declared
  a second time in `pyproject.toml`.

### Documentation

- The README opens by saying what Hermes Agent and Hookdeck Event Gateway are,
  rather than assuming both.
- It also says *which* Hookdeck. This is the Event Gateway — inbound events
  arriving at your agent — and not Outpost, which is the other direction. And
  "platform" in `kind: platform` is Hermes' word for a source of inbound work,
  not a reference to the Hookdeck platform.
- A new architecture section with two diagrams: topology, and a sequence diagram
  for the ack-then-hand-back contract that the reliability rests on. The three
  different things called "CLI" are separated there.
- A new section listing Hookdeck capabilities the plugin does not currently use
  — the Publish API, bulk operation plans and cancellation, request replay,
  ignored-event retry, issue triggers, transformations, the wider metrics — so
  the edge of `hookdeck/api.py` is not mistaken for the edge of the product.
- The "no pull API" limitation now explains what the durability claim actually
  rests on: an event is recoverable because a delivered-but-failed event stays
  retryable, not because anything holds a lease on it.

## 0.1.0

First release. Hookdeck platform adapter, `hermes hookdeck` operator commands,
the `hookdeck` agent toolset, the `triage-webhook-failures` skill and the
dashboard tab.
