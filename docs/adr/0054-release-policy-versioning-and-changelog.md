# Version every user-visible change and ship it with notes

## Context

`docs/behavioral_contract.md` ch. 33 (Compatibility and Upgrade Behavior) requires that behavior changes between versions be defined and that auto-fix semantics not change silently in a patch release. `docs/adr/0027-behavioral-contract-audit-synthesis.md` closed the non-caching part of that chapter as "not applicable to a solo, no-external-consumer hobby project with no CI and no formal release process". [Issue #133](https://github.com/alessio-locatelli/ruff-extra-rules/issues/133) retires that premise: the hooks are consumed externally by `rev:` pin, and a consumer has nothing to read before moving the pin.

Nothing about the compatibility stance itself is in question. `AGENTS.md`'s "breaking changes are allowed and expected" stands, and issue #133 rules out shims and deprecation layers. The gap is that a change arrives unannounced, in a version number that carries no information: every tag was a patch bump, every release body was empty, and the three places a version was written down disagreed with each other and with the newest tag.

## Considered Options

- **Keep patch-only tags and rely on ADRs.** Rejected by the issue. An ADR records why the architecture is what it is, for someone working on it; it does not tell someone upgrading whether their build is about to fail.
- **Declare the surface stable and release 1.0.0.** Rejected. It promises a stability the project does not claim (the README calls it alpha), and a linter that keeps adding checks would then ship a major release for most of them, which drains the signal a major is supposed to carry.
- **`0.MINOR.PATCH`.** Adopted.
- **Generate notes from the Conventional Commit subjects gitlint already enforces.** Rejected as the mechanism. The subjects are written for whoever maintains the code; a subject line cannot say which inputs a fix now rewrites differently, which is precisely what a consumer needs.
- **Hand-maintained changelog in Keep a Changelog format.** Adopted.
- **Publish to PyPI.** Rejected for now: consumption is by git tag, so a PyPI project would add a name to hold and a trusted publisher to maintain for no consumer that exists. Nothing here forecloses adding it later.

## Decision

Versions are `0.MINOR.PATCH`. MINOR covers every change that can make a run come out differently on code the user has not touched; PATCH is everything a passing run cannot observe. `docs/releases.md` states the rule and enumerates the surface it covers, and is the user-facing half of this decision.

`pre_commit_hooks.__version__` is the single place a version is written; the package metadata derives from it.

`CHANGELOG.md` carries the notes, edited in the same change that alters behavior rather than assembled at release time. A release moves the accumulated `Unreleased` section into a dated one for the version being cut, so the notes and the version are decided together and cannot disagree.

Pushing a `v*` tag runs the release: the test suite, a build of both distributions, a cross-check that the tag, the package metadata and the newest changelog section name the same version, an install-and-run check of the built wheel, and then a GitHub release whose body is that version's changelog section with both distributions attached. The version half of that cross-check also runs in the ordinary test suite, so `__version__` and the newest changelog section cannot drift apart between releases.

ADRs remain architecture records. A changelog entry may link one for the reasoning; it may not stand in for one, and an ADR may not stand in for the entry.

## Consequences

- Most user-visible changes are MINOR, so the number moves quickly and says little beyond "read the notes". That is the intended reading: the alternative is a number that never moves and says nothing at all.
- The workflow can check that a version has notes, not that they are true or complete. Their quality rests on the discipline of writing them alongside the change, which is why they are not deferred to release time.
- Tags before 0.0.50 have no notes and are not retro-fitted.
- An incompatible change to the `[tool.ruff-extra-rules]` keys (`docs/adr/0045-pyproject-toml-configuration.md`) is versioned and announced like any other, with no reader that converts an older file — issue #133 rules that out. Ch. 33's "migration path for incompatible configuration changes" is therefore met by the notes alone, which is a weaker guarantee than the chapter's wording suggests and a deliberate one.
- **Supersedes** `docs/adr/0027-behavioral-contract-audit-synthesis.md` on ch. 33's remaining disposition: it is closed by this policy rather than judged not applicable.
