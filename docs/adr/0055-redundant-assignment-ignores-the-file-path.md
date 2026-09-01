# TR5 judges an assignment without consulting the file's path

TR5 scored a variable's semantic value differently depending on the file's location in the tree: a path containing a `tests`/`test` component, or a `test_*.py`/`*_test.py` filename, raised the score of descriptive names enough to suppress reports that the identical code in `src/` would get. ADR-0007 recorded that as deliberate on the grounds that this project had no outside consumers and its own layout matched the hardcoded convention.

That premise no longer holds (see ADR-0054), and a project laid out as `spec/` or `__tests__/` had no way to state its intent. Issue [#135](https://github.com/alessio-locatelli/ruff-extra-rules/issues/135) asked for a configuration surface to name those directories.

## Decision

Whether an assignment is a violation is decided from the code alone. The heuristic is removed rather than made configurable, and no new configuration key is added. The path still decides which files TR5 runs on — `exclude` (ADR-0046) and `per-file-ignores` (ADR-0049) are unaffected — but once TR5 is given a file, where that file sits changes nothing about the answer.

The default level, `conservative`, exists to report only assignments that are redundant on the code's own terms — not ones that merely lose a style argument. A relaxation that suppresses reports in one directory is therefore either hiding real violations there, or admitting that the suppressed reports were never sound to begin with; a path is not evidence about an assignment. Both readings are defects at the default level, and neither is fixed by letting a project spell the directory differently.

Wanting a check quieter in a subtree remains a real need. It is answered by settings that already exist and are not specific to one rule: `per-file-ignores` (ADR-0049) switches TR5 off for a matching pattern, and a project that only wants the _broader_ set of reports outside its tests can scope `--redundant-assignment-level=aggressive` to a second hook entry with its own `files:`.

## Considered Options

- **Make the directory convention configurable, as issue #135 proposed** — a `pyproject.toml` key naming the test paths, or deriving them from `[tool.pytest.ini_options] testpaths`: rejected. It preserves the defect and adds a key to maintain, a validation surface, and a cache-identity input, all to let a project pick which files get the less accurate answer. ADR-0049 already provides the general form of "this check, not these files."
- **Keep the relaxation for `aggressive` only**: rejected. It leaves the hardcoded path convention in the codebase, so the issue's complaint survives for the level that reports the most — and `aggressive` is precisely the level whose reports are already understood to be arguable, where an author's own judgement, not a directory name, is what decides.
- **Keep the relaxation and document it** (ADR-0007's decision): superseded.

## Consequences

- A file whose path or name matched the old convention can now report violations it did not before, at either level. TR5's own scoring is otherwise untouched, so the newly reported assignments are the ones the rest of the rules already judge redundant.
- One class of latent cache bug is gone by construction: a cached TR5 result can no longer be wrong for the file's content because the file moved.
- ADR-0007 is superseded and annotated rather than rewritten, per the convention in ADR-0042 and ADR-0044.
