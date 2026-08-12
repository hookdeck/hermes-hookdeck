# Release notes template

A skeleton, not a form. **Delete every heading you have nothing real to put
under** — a section reading "None" is worse than no section.

Order is deliberate: anything the reader must act on comes first, so it is not
buried under a list of fixes.

---

```markdown
<!-- One or two sentences: what this release is for. Skip for a single-fix
     patch where the heading below says it already. -->

## Upgrading

<!-- ONLY when the reader must do something: a renamed config key or
     HOOKDECK_EG_* variable, a changed default, a new required setting, a
     minimum Hermes version. Say what to change, not just that something
     changed.

     If nothing is required, delete this section and say "drop-in upgrade"
     in the summary instead. -->

## Breaking changes

<!-- MAJOR only. What stops working, and what to do about it. -->

## New

<!-- New config options, subcommands, agent tools, route features.
     One bullet each, phrased as what it lets the reader do. -->

## Fixed

<!-- Only fixes the reader could plausibly have hit. Describe the symptom
     from outside — "the bundled skill never loaded", not "passed a str to
     register_skill". The commit has the mechanism; this has the impact. -->

## Also

<!-- Packaging, dependencies, metadata. Things worth recording and not worth
     a section of their own. Omit entirely if the answer is "nothing". -->

**Full changelog**: https://github.com/hookdeck/hermes-hookdeck/compare/vPREV...vNEW
```

---

## Notes on the sections

**Upgrading vs Breaking changes.** They are not the same. A MINOR release can
need an upgrade note (a new option you should probably set) without breaking
anything. A MAJOR release needs both.

**"Fixed" is about symptoms.** The reader did not read the diff and does not
know the internals. `hookdeck/skills/` corrections are worth a bullet because
the skill ships in the wheel and its behaviour is visible; a refactor with no
behavioural change is not.

**Docs-only releases usually should not exist.** `docs/`, `README.md`,
`AGENTS.md` and the root `skills/` directory are not packaged, so a release
containing only those gives a user nothing to install. Check with
`git diff --stat PREV_TAG..origin/main -- hookdeck/` before drafting.

**Contributors.** Only for a first-time contributor, or a contribution large
enough that its absence would be conspicuous. Not every release, and not for
regular maintainers.
