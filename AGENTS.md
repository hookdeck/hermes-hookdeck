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

## The version is the git tag

Nothing in this repo declares a version. setuptools-scm derives it from the tag
at build time and writes `hookdeck/_version.py` into the wheel, so the PyPI
version, `hookdeck.__version__` and what `hermes plugins list` reports are the
same string by construction.

**Do not write a version literal anywhere.** Three copies existed before this
rule and all three had drifted: `plugin.yaml` and `dashboard/manifest.json`
both said `0.1.0` while the package was at `0.1.1`, and each was invisible
enough that nobody noticed.

`plugin.yaml`'s key is gone — nothing reads it for an entrypoint install.
`dashboard/manifest.json` genuinely needs one, because Hermes reads that file
off disk to display the plugin's version, so it is **generated at build time**
by `_build/hermes_build.py` and is absent from the tracked file. CI fails the
build if a wheel's manifest disagrees with the wheel's own version.

If you find yourself adding a third place the version must appear, generate it
the same way rather than typing the number.

Between releases a checkout reports `0.1.2.dev4+g1a2b3c4` — "four commits past
v0.1.1". That is correct, not a placeholder to fill in.

## Skills

Two directories here are called "skills" and they are not the same thing.
Check which one you are in before editing:

| | `hookdeck/skills/` | `skills/` (repo root) |
|---|---|---|
| Audience | The agent running in a gateway | Whoever maintains this repo |
| Ships in the wheel | **Yes** — so editing one is a release | No |
| Registered with Hermes | Yes, as `hookdeck:<name>` | No |
| Example | `triage-webhook-failures` | `hermes-hookdeck-release` |

`.claude/skills`, `.cursor/skills` and `.agents/skills` are symlinks to the
root `skills/`, so each agent finds the maintainer skills where it expects
them. They are symlinks rather than copies deliberately — a copy drifts, and
nothing here would catch it.

The consequence worth remembering: **a one-word fix to
`hookdeck/skills/triage-webhook-failures/SKILL.md` is a patch release**, while
a rewrite of this file is not. `skills/hermes-hookdeck-release` has the rule.

### Where a Hookdeck skill belongs

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
