# Working in this repo

A Hermes Agent plugin for the Hookdeck **Event Gateway**. `docs/development.md`
has the code layout and how to run the tests; this file covers the things an
agent gets wrong without being told.

## Hookdeck product information: link, do not restate

The official docs are the source of truth for anything about the product
itself — retries, replay, ignored events, pause semantics, connection rules,
plan retention. This repo is the source of truth only for what *this plugin*
does with them.

So when you find yourself explaining a Hookdeck behaviour, link it instead:

| Topic | Source of truth |
|---|---|
| Retries, manual retry, dead-letter strategy | <https://hookdeck.com/docs/retries> |
| Requests, replay, ignored events | <https://hookdeck.com/docs/requests> |
| Connections and their rules | <https://hookdeck.com/docs/connections> |
| Pausing and resuming a connection | <https://hookdeck.com/docs/guides/how-to-pause-connections> |

Restated product semantics rot silently: nothing here fails when Hookdeck
changes a behaviour, so a wrong sentence survives every test run and every
review. A link cannot go stale in that way.

Two things are worth stating rather than linking, and both are about *this*
plugin rather than the product: the exact code or field the plugin keys off
(an agent following a procedure needs the literal `error_code`, not a page
that mentions it), and behaviour the docs do not cover because it only arises
here. Say the fact, then link the page.

## Skills

`hookdeck/agent-skills` is the home for Hookdeck skills:
<https://github.com/hookdeck/agent-skills>. Its purpose is to point an agent at
the official docs as the source of truth, and it is distributed to any agent
that supports the Agent Skills spec — `npx skills add hookdeck/agent-skills`.

**Before authoring or updating a skill in this repo, read what is already
there.** If the thing you are about to write is about Hookdeck rather than
about this plugin, it belongs in `agent-skills`, not here — and probably
already exists.

A skill earns its place in this repo only when it is coupled to what the plugin
itself provides: the `hookdeck_*` tools in `hookdeck/tools.py`, the ledger, the
adapter's behaviour. `skills/triage-webhook-failures` qualifies because every
actionable step calls one of those seven tools; installed anywhere else it
would tell an agent to call tools that do not exist.

That coupling is also the maintenance rule. A skill here names identifiers this
package owns, so renaming a tool means editing its documentation in the same
commit. Nothing catches the drift otherwise.

Hermes registers these as **plugin skills**: namespaced (`hookdeck:<name>`),
kept out of the flat `~/.hermes/skills/` tree and out of the system prompt's
skill index, and loaded only when something asks for them by name. They are not
discoverable by an agent browsing its skills, so a skill here is only reachable
if a route prompt or an operator names it.

## Verification

The suite runs against `tests/hermes_stub.py`, which this repo also owns — so it
agrees with the plugin by construction and cannot notice upstream renaming
something underneath it. `scripts/check_upstream_contract.py` is the smoke
alarm for names; a real `hermes gateway run` is the only thing that proves
integration. Both of the defects found after 200 green tests — the skill that
never registered, and the triage skill's confidently wrong root cause — came
from real runs, not from the suite.

When you change something on the delivery path, say which of those three levels
you actually exercised.
