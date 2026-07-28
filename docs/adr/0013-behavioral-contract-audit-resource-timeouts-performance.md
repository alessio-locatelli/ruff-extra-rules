# Behavioral contract audit: resource usage, timeouts & performance (ch. 24, 25, 30)

`docs/behavioral_contract.md` chapters 24 (Resource Usage), 25 (Timeouts and Hanging Operations), and 30 (Performance) were audited against `CacheManager`, `_prefilter.py`'s `git grep` subprocess call, and every check's own AST traversal. Full findings, including exact reproductions and measured before/after timings, are in `docs/audits/0004-behavioral-contract-audit-resource-timeouts-performance.md`.

## Decision

Two correctness gaps and one performance gap crossed the bar for a fix:

- `CacheManager._locked()`'s advisory cross-process lock (guarding concurrent hook processes racing on the same cache blob) blocked indefinitely with no timeout. It now polls a non-blocking lock attempt against a 10-second deadline and raises `TimeoutError` — a builtin `OSError` subclass every existing caller already treats as an ordinary cache failure, degrading to an uncached result rather than hanging.
- `validate_function_name`'s `attach_parents()` used unbounded hand-written recursion over the whole file's AST, and could hit `RecursionError` on ordinary (if unusually deep) valid Python well before `ast.parse()` itself would fail. Rewritten to use an explicit stack instead of recursion; same traversal and output, no depth limit.
- `meaningless_vars` and `redundant_assignment` each called `ast.get_source_segment()` once per assignment node, which re-splits the entire source into lines internally on every call — O(assignments × source size). A precomputed-line-list fast path (`fast_get_source_segment()`) cut a synthetic 2000-function file from 3.5s/2.6s down to ~0.12s/~0.32s in the two checks, with a byte-for-byte equivalence test against the original `ast.get_source_segment` behavior, not just a speed assertion.

The other checks built on `ast.NodeVisitor` (and several narrower hand-rolled recursive helpers) share the same unbounded-recursion shape as `attach_parents()` did, but are already safe in the sense that matters here: a `RecursionError` in any of them is caught by the existing per-check exception isolation (ADR 0012), reported, and forces a non-zero exit — never a silent or corrupted result. Rewriting `ast.NodeVisitor`'s own recursive traversal across five checks would be a fundamental architecture change, disproportionate to an audit-scoped fix; only `attach_parents()` (unconditional, whole-file, trivially convertible) was rewritten.

Several other candidate gaps (unbounded cache growth, no diagnostic streaming, no explicit large-file size cap) were judged acceptable: they match the same tradeoffs `mypy`/`ruff` themselves make, and none had a concrete failure mode left after the performance fix above.

## Consequences

- `CacheManager._locked()` can now raise `TimeoutError`; every existing caller already handles it via its existing `except OSError`.
- `_base.py` gains `fast_get_source_segment()` and `split_lines_like_ast()` (the latter needed because `str.splitlines()` splits on characters — form feed and others — that Python's own tokenizer treats as ordinary content, which the first version of the fast path got wrong).
- `meaningless_vars`'s `MeaninglessNameVisitor` and `redundant_assignment`'s `VariableTracker` each precompute a second line list (`self._ast_lines`) for AST-consistent line splitting, alongside the pre-existing `self.source_lines`.
