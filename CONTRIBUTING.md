# Contributing

PRs are welcome! If you plan to contribute a major change, please open an issue or discussion first to ensure we share a common understanding of the intended submission. See `AGENTS.md` for linting requirements, testing guidelines, and the expected quality baseline.

For the technical walkthrough of how checks are built (if you're forking this or just curious), see [docs/adding-a-check.md](docs/adding-a-check.md).

## Cutting a release

[docs/releases.md](docs/releases.md) is the policy this checklist implements; read it first to pick the number.

1. Move the `## [Unreleased]` notes in [CHANGELOG.md](CHANGELOG.md) into a `## [X.Y.Z] - YYYY-MM-DD` section.
2. Set `__version__` in `src/pre_commit_hooks/__init__.py` to the same version. Nothing else records it.
3. Commit, then tag `vX.Y.Z` and push the tag.

The tag runs `.github/workflows/release.yaml`, which refuses to publish unless the tag, the built distributions and the newest changelog section agree, and then creates the GitHub release from that section. To check before tagging:

```bash
uv build
uv run python -m release.validate vX.Y.Z
```

## Performance benchmarks

The benchmark suite measures the direct ast-check invocation and the equivalent local `prek` hook against small, typical, and large Python files. Each path is measured with an empty check cache and with a populated one.

Install the standalone `prek` executable before running these commands; see the [official installation documentation](https://prek.j178.dev/installation/).

```bash
uv run pytest tests/performance --benchmark-only --benchmark-json=benchmark-results.json
uv run python -m benchmarks.regression benchmark-results.json benchmarks/baseline.json
```

CI uploads the JSON report and adds the comparison to its job summary. A candidate is a review signal, not an automatic failure: reproduce it locally with the same command before deciding whether it is a real regression.

To change `baseline.json`, run the suite, update every affected value, and include a `Performance baseline rationale:` section in the pull request body that explains the intentional trade-off. CI requires that section whenever the baseline changes.
