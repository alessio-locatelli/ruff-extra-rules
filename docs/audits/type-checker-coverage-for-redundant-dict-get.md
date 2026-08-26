# Type-checker coverage for redundant-dict-get

## Question

Does a supported type checker already report a redundant `.get()` where local code proves the key exists?

## Method

On 2026-08-26, run each installed checker against one file containing:

- a local dict display followed by `.get("port")`;
- a required TypedDict key followed by `.get("port")`;
- a `key not in mapping` return guard followed by `.get(key)`.

The return types allow `None`, so an ordinary optional-result type error is not involved.

## Results

| Checker | Version | Result         |
| ------- | ------- | -------------- |
| ty      | 0.0.74  | No diagnostics |
| Mypy    | 2.3.0   | No diagnostics |
| Pyright | 1.1.411 | No diagnostics |

None emitted a redundancy or style diagnostic for these proven-presence calls. TR9 therefore owns the invariant-expression diagnostic rather than duplicating a type error.
