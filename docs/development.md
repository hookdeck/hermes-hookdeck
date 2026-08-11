# Development

How to run the tests, and what lives where.

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
```

```bash
.venv/bin/python -m pytest
```

The tests stub the Hermes internals the adapter imports (`tests/hermes_stub.py`)
so the ingest path — verification, dedup, admission control, ack modes, outcome
reporting — is exercised without a Hermes checkout.

## Code layout

| Module | What lives there |
|---|---|
| `adapter.py` | The platform adapter: lifecycle, the delivery pipeline, outcome reporting |
| `settings.py` | Every knob, resolved once from config + env, validated before start |
| `routing.py` | Which route a delivery belongs to, and what event it is |
| `payload.py` | Bytes → payload, including the encoding rules that bite |
| `verify.py` | The one signature scheme |
| `state.py` | The SQLite delivery ledger |
| `api.py` | A thin async client for the Hookdeck API |
| `provision.py` | Building the connection Hookdeck should have |
| `cli.py` | `hermes hookdeck …` |
| `tools.py` | The agent-facing toolset |
| `dashboard/` | The dashboard tab: manifest, backend routes, and a no-build bundle |

`routing.py`, `payload.py`, `verify.py`, `provision.py` and `settings.py` are
pure — no Hermes, no HTTP, no state — so the rules they encode can be read and
tested on their own. `adapter.py` is the only module that needs a gateway.
