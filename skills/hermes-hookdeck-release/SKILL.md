---
name: hermes-hookdeck-release
description: >-
  Guides maintainers through releasing the hermes-hookdeck plugin to PyPI.
  Publishing a GitHub release is what triggers the publish, so the version on
  main must already match the tag. Validates the proposed version against
  SemVer from the actual change set, including the rule that a change to the
  bundled skill or plugin.yaml is a shipped change. Use when cutting a release,
  publishing to PyPI, drafting release notes, choosing vMAJOR.MINOR.PATCH,
  `gh release create`, or following the release checklist.
---

# hermes-hookdeck — release workflow

## Canonical documentation

Follow **[README.md](../../README.md) § Releasing** for the human steps
(GitHub UI). This skill adds **how the automation works**, the **gates that
must pass first**, and a **research loop** for drafting notes.

## The one thing that goes wrong

`hookdeck/__init__.py` declares `__version__`, and the release tag must equal
`v` + that value. The workflow checks this **first** and fails the build if
they disagree.

So the version bump is a **commit on `main`, merged before the release is
created** — not something you do while releasing. Publishing a release for a
version that is not yet on `main` fails, and the only way out is deleting the
release *and* its tag and starting again.

**PyPI is append-only.** A version number that has been published can never be
reused or replaced, even after a yank. Getting the number right before you
publish matters more than any other step here.

## Agent checklist (end-to-end)

Follow **in order**. Items marked **gate** are blocking unless the maintainer
explicitly overrides.

- [ ] **Everything intended for the release is merged to `main`.** Nothing is
      released from a branch.
- [ ] **`PREV_TAG` / `NEW_TAG` confirmed** — `git tag --sort=-v:refname | head -1`
      for the current release; propose `NEW_TAG` and agree it.
- [ ] **Change set reviewed:** `git log PREV_TAG..origin/main` — read the full
      messages, not just subjects. Group for **user-facing** notes.
- [ ] **gate — SemVer:** `NEW_TAG` matches the **minimum** bump for the delta
      (see **SemVer** below). Stop and realign if under-bumped.
- [ ] **gate — version bump merged:** `hookdeck.__version__` on `origin/main`
      equals `NEW_TAG` without the `v`. If not, that bump is its own commit,
      merged first.
- [ ] **gate — CI green on `main`:** the tip of `main` has passing checks.
- [ ] **Release notes drafted** — see **Drafting release notes** and
      [references/release-notes-template.md](references/release-notes-template.md).
- [ ] **gate — maintainer approval** of tag name and notes. No surprise
      releases; publishing is irreversible on PyPI.
- [ ] **Publish** with `gh release create` (below), targeting `main`.
- [ ] **Confirm** the `Release` workflow succeeded and the version is on PyPI.

## What triggers a release

**Publishing a GitHub release** — [.github/workflows/release.yml](../../.github/workflows/release.yml)
runs on `release: [published]`. There is no separate approval step: publishing
the release *is* the decision to ship.

Pushing a bare tag does **nothing**. The tag is created by the release.

## What the workflow does

1. **build** — checks out the release's tag, asserts the tag matches
   `hookdeck.__version__`, runs `ruff` and the full test suite, builds the
   wheel and sdist, and runs `twine check`.
2. **publish** — uploads to PyPI via Trusted Publishing (OIDC, no stored
   token), in the `pypi` environment.
3. **attach-artifacts** — attaches the wheel and sdist to the GitHub release
   you created, so it carries the artifacts that actually went to PyPI.

A failure in **build** means nothing was published; fix `main` and publish a
new release. A failure in **publish** can be re-run from the Actions tab
(`gh run rerun <id> --failed`) — do not create a second release for the same
version.

## SemVer: validate the proposed version

Check any proposed tag against what actually changed since `PREV_TAG`.

**The contract this package offers** is its configuration, its environment
variables, its CLI, its agent tools, and the Hermes surfaces it registers.

| Change since `PREV_TAG` | Bump | Examples |
|---|---|---|
| **Breaking** — an existing install stops working, or behaves differently without the operator changing anything | **MAJOR** | Renamed or removed config key or `HOOKDECK_EG_*` variable; a default that changes delivery behaviour; a removed `hermes hookdeck` subcommand; a renamed or removed `hookdeck_*` agent tool; a ledger schema change that an older row cannot satisfy |
| **New capability**, backward compatible | **MINOR** | New config option with a safe default; new subcommand or agent tool; a new route feature; support for a Hermes version that was not supported before |
| **Fixes and corrections**, no new capability, nothing breaks | **PATCH** | Bug fixes; corrections to the bundled skill or `plugin.yaml`; dependency bumps with no behaviour change; packaging metadata |

**A change under `hookdeck/` ships even when it is only prose.** The bundled
skill (`hookdeck/skills/`), `plugin.yaml` and the dashboard bundle are inside
the wheel, so correcting them is a **patch release**, not a docs-only change.

**A change outside `hookdeck/` usually does not ship.** `docs/`, `README.md`,
`AGENTS.md`, `tests/`, `skills/` at the repo root and `.github/` are not
packaged. A release containing only those has nothing for a user to install —
say so rather than cutting one.

Verify which of the two you are in rather than assuming:

```bash
git diff --stat PREV_TAG..origin/main -- hookdeck/
```

Empty output means nothing shipped.

**Agent behaviour:** state the **minimum** bump the change set requires, then
compare it to what was proposed. If they conflict, **do not treat the proposal
as authoritative** — explain the mismatch and recommend the correct version. If
it is genuinely ambiguous whether something breaks an existing install, **ask**.

## Publish with GitHub CLI (`gh`)

Create the release with `gh` rather than pushing a tag — a bare tag does not
trigger anything.

1. **Write the notes to a temp file** and register cleanup, so a failure does
   not leave it behind:

   ```bash
   NOTES_FILE="$(mktemp "${TMPDIR:-/tmp}/hermes-hookdeck-release-notes.XXXXXX.md")"
   trap 'rm -f "$NOTES_FILE"' EXIT
   ```

2. **Write** the final markdown body to `"$NOTES_FILE"`.

3. **Create the release**, targeting `main`:

   ```bash
   gh release create "vM.m.p" \
     --repo hookdeck/hermes-hookdeck \
     --target main \
     --title "vM.m.p" \
     --notes-file "$NOTES_FILE"
   ```

   Add `--prerelease` for `rc`, `a` or `b` versions, so the repository's
   "latest release" does not point at a release candidate.

4. **Watch it**: `gh run watch` — or `gh run list --workflow=release.yml --limit 1`.

**Requirements:** `gh` authenticated, and the version bump already on `main`.
Do not put secrets in the notes file.

### Verify afterwards

```bash
pip download --no-deps -d /tmp/verify hermes-hookdeck==M.m.p
```

For a release that changed anything under `hookdeck/`, confirm the change is
really in the artifact rather than only in the repo — install it into a clean
venv and check the file, not the working tree.

## Drafting release notes

Use [references/release-notes-template.md](references/release-notes-template.md)
as the skeleton. **Include only headings with real content** — omit a section
rather than writing "None".

Write for someone who runs a gateway, not for someone who reads this repo. The
useful question is "what changes for me, and do I have to do anything?" — not
"which files moved".

- **Lead with anything that requires action.** Config changes, renamed
  variables, anything that alters delivery behaviour. If nothing does, say the
  upgrade is a drop-in.
- **A fix is only worth a bullet if the reader could have hit it.** Say what
  went wrong from the outside ("the bundled skill never loaded"), not what the
  code did.
- Always end with the compare link:
  `https://github.com/hookdeck/hermes-hookdeck/compare/<prev_tag>...<new_tag>`

**Contributors:** do not add a generic thanks block every release. Include one
only for a first-time contributor or an exceptionally large contribution.

## Research loop

1. **Tags:** `git tag --sort=-v:refname | head -5`. Confirm `PREV_TAG`.
2. **Commits:** `git log PREV_TAG..origin/main` — full messages. This repo
   writes the reasoning into commit bodies; that is the changelog source.
3. **Shipped or not:** `git diff --stat PREV_TAG..origin/main -- hookdeck/`.
4. **Group** by user impact, not by file.
5. **SemVer check** against the table above.
6. **CI:**

   ```bash
   gh api "repos/hookdeck/hermes-hookdeck/commits/$(git rev-parse origin/main)/status" --jq .state
   ```

   Do not release on `failure`, or on `pending` for required checks.

## Safety and governance

- **PyPI is append-only.** A published version cannot be reused, replaced, or
  truly deleted. Yanking hides it from resolvers; it does not free the number.
- **Do not release a red `main`.** The workflow runs the suite itself and will
  fail the build, but finding out during a release is the wrong time.
- **Do not under-bump.** Resolve a SemVer disagreement with the maintainer
  before publishing, not after.
- **Do not publish a release for a version not yet on `main`** — it fails, and
  recovering means deleting both the release and its tag.
- **Green tests are not proof of integration.** The suite runs against a stub
  this repo also owns. Both defects found after 200 green tests came from a
  real `hermes gateway run`. For a release touching the delivery path, say
  which level you actually exercised — see [AGENTS.md](../../AGENTS.md).

## Related files

| Topic | Location |
|---|---|
| Human release steps | [README.md § Releasing](../../README.md) |
| CI entrypoint | [.github/workflows/release.yml](../../.github/workflows/release.yml) |
| Version declaration | [hookdeck/\_\_init\_\_.py](../../hookdeck/__init__.py) |
| What is packaged | [pyproject.toml](../../pyproject.toml) `[tool.setuptools.package-data]` |
| Repo conventions | [AGENTS.md](../../AGENTS.md) |
