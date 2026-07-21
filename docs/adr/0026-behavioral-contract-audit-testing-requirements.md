# Behavioral contract audit: testing-requirements audit (ch. 31, 32)

`docs/behavioral_contract.md` chapters 31 (Testing Requirements) and 32 (Testing the Auto-Fix Pipeline) were audited against the current test suite (382 test functions, 812 collected test cases across parametrization), sequenced after `0011`'s fix-engine audit (issue #30), `0023`'s semantic/formatting-preservation audit (issue #39), and `0025`'s determinism/idempotence audit (issue #40), so it targets the corrected, settled behavior from those tickets rather than a pre-fix baseline. Full findings, including the complete item-by-item mapping of every ch. 31/32 bullet to its covering test(s), are in `docs/audits/0015-behavioral-contract-audit-testing-requirements.md`.

## Decision

Most of both chapters were already satisfied — this project's test suite is unusually thorough as a byproduct of `0001`/`0002`/`0011`/`0014`/`0023`/`0025`'s own bug-hunting audits, each of which added its own regression tests as it went. Four genuine gaps were found and fixed, all in test coverage rather than behavior:

- No test exercised a large file end-to-end; the only prior evidence of large-file correctness was an uncommitted manual benchmark. Added a correctness-focused (not performance-timing) regression test covering a large generated file.
- No test used a path containing spaces or Unicode characters, even though the underlying requirement (ch. 13: paths are always passed as plain subprocess/filesystem arguments, never through a shell) was already satisfied by design and only ever verified ad hoc. Added a regression test that exercises the real `git grep` subprocess call this pipeline's prefilter makes for such a path, inside a real git repository, and confirms the path itself reaches that call intact — not just that the pipeline's end-to-end result happens to be correct, which a silent fallback path could also produce.
- The one test covering a _successful_ `--fix` run's printed CLI output never checked the file's actual final content, unlike its rejected/errored/failed sibling tests — the one case ch. 32's "reported result must match the actual final file state" names that had no matching assertion. Extended it with a content check.
- One test was a direct instance of the anti-pattern ch. 31 explicitly prohibits ("MUST NOT consider a test that merely checks that the process did not crash sufficient evidence of correctness") — it asserted nothing beyond the absence of an exception. Strengthened it to assert the actual degraded-but-correct end state.

Full findings, including the complete item-by-item mapping of every ch. 31/32 bullet to its covering test(s) and the reasoning behind each of the four fixes above, are in `docs/audits/0015-behavioral-contract-audit-testing-requirements.md`.

## Consequences

- `tests/test_orchestrator.py` and `tests/test_main.py` gain/extend the four regression tests described above.
- No production code changed — every gap here was in test coverage, not behavior.
- Two ch. 31 items (serial/parallel execution; all officially supported operating systems) were judged not applicable, with rationale recorded in `docs/audits/0015` rather than fixed: this project has no in-process parallelism to test in the first place (`0007`), and Linux is its only officially supported platform (`AGENTS.md`, `0020`), so the entire suite already runs exclusively on the one supported target.
