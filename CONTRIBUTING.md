# Contributing

This is a personal hobby project maintained solo, entirely through coding agents. It isn't set up to take third-party pull requests.

Opening an issue is welcome — bug reports, questions, suggestions — and will be looked at on a best-effort basis, with no guaranteed response time.

For the technical walkthrough of how checks are built (if you're forking this or just curious), see [docs/adding-a-check.md](docs/adding-a-check.md).

## Performance benchmarks

The benchmark suite measures the direct ast-check invocation and the equivalent local `prek` hook against small, typical, and large Python files. Each path is measured with an empty check cache and with a populated one.

Install the standalone `prek` executable before running these commands; see the [official installation documentation](https://prek.j178.dev/installation/).

```bash
uv run pytest tests/performance --benchmark-only --benchmark-json=benchmark-results.json
uv run python -m benchmarks.regression benchmark-results.json benchmarks/baseline.json
```

CI uploads the JSON report and adds the comparison to its job summary. A candidate is a review signal, not an automatic failure: reproduce it locally with the same command before deciding whether it is a real regression.

To change `baseline.json`, run the suite, update every affected value, and include a `Performance baseline rationale:` section in the pull request body that explains the intentional trade-off. CI requires that section whenever the baseline changes.
