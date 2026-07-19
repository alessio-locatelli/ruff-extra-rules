# Auto-Fixing Python Linter: MUST and MUST NOT Checklist

This checklist defines the behavioral and engineering requirements for a Python linter that can automatically modify source code.

The requirements apply to the linter as a whole, including its command-line interface, rule engine, auto-fixer, file handling, caching, parallel execution, configuration, and integrations.

A requirement is a **MUST** when violating it can cause incorrect results, data loss, corruption, silent failures, or a materially broken user experience.

A requirement is a **MUST NOT** when the behavior is inherently unsafe, misleading, or incompatible with reliable automation.

---

## 1. Correctness and Safety of Auto-Fixes

- **MUST** ensure that every auto-fix either produces the intended transformation or leaves the source unchanged.
- **MUST** validate the result of an auto-fix before replacing the original source file.
- **MUST** reject a fix that produces syntactically invalid Python.
- **MUST** preserve the original file if a fix cannot be safely applied.
- **MUST NOT** leave a file partially modified when an auto-fix fails.
- **MUST NOT** silently apply a fix whose result cannot be validated.
- **MUST NOT** report a fix as applied when the file was left unchanged.
- **MUST** report when a proposed fix was rejected because validation failed.
- **MUST** ensure that a fixer does not accidentally modify source outside its intended range.
- **MUST** ensure that a fixer does not silently overwrite unrelated concurrent changes.
- **MUST NOT** rely solely on the validity of an individual text edit when the combined result of multiple edits can be invalid.
- **MUST** validate the final combined output when multiple fixes are applied to the same file.
- **MUST** handle overlapping or conflicting fixes deterministically.
- **MUST NOT** apply conflicting edits in an undefined order.
- **MUST** either resolve conflicting fixes deterministically or reject the conflicting set safely.
- **MUST NOT** silently discard a valid fix merely because another unrelated fix failed, unless the behavior is explicitly defined.
- **MUST** ensure that a fix cannot repeatedly reintroduce the diagnostic it was intended to fix.
- **MUST** make each fix idempotent whenever reasonably possible.
- **MUST NOT** cause repeated fix runs to continually modify the same source without a genuine source change.
- **MUST** ensure that applying fixes repeatedly eventually reaches a stable state.
- **MUST** distinguish between a diagnostic that was fixed and a diagnostic that disappeared as a side effect of another fix.
- **MUST NOT** claim that a rule was fixed when the resulting code still violates the same rule, unless the tool explicitly reports that the fix was incomplete.

---

## 2. Preservation of Source Semantics

- **MUST** preserve program semantics unless the rule explicitly intends a semantic transformation.
- **MUST NOT** perform an auto-fix that can change runtime behavior without the rule explicitly defining that behavior change.
- **MUST** preserve comments unless the rule explicitly owns the relevant comment.
- **MUST** preserve string contents unless changing them is explicitly part of the rule.
- **MUST** preserve meaningful whitespace where whitespace affects Python semantics.
- **MUST** preserve decorators, type comments, pragmas, and other semantically significant source constructs.
- **MUST** preserve encoding declarations when they remain applicable.
- **MUST** preserve source constructs that are not owned by the fixer.
- **MUST NOT** reconstruct a file from a lossy representation if that can discard comments, formatting, encoding, or other source information.
- **MUST** make transformations based on the actual parsed source rather than unreliable textual assumptions whenever syntax-level correctness matters.
- **MUST** ensure that a fix does not change the meaning of an f-string, string literal, escape sequence, or formatted expression unintentionally.
- **MUST** ensure that a fix does not change operator precedence unintentionally.
- **MUST** ensure that a fix does not change evaluation order unintentionally.
- **MUST** ensure that a fix does not change name binding or scope unintentionally.
- **MUST** ensure that a fix does not change import behavior unintentionally.
- **MUST** ensure that a fix does not change exception behavior unintentionally.
- **MUST NOT** assume that syntactically valid code is necessarily semantically equivalent to the original code.

---

## 3. Source File Integrity

- **MUST** avoid corrupting the original file if any part of the fix operation fails.
- **MUST** write files atomically whenever the platform and filesystem support it.
- **MUST** avoid leaving a truncated file after a failed write.
- **MUST** avoid leaving an empty file after a failed write.
- **MUST** preserve the original file when the replacement cannot be completed safely.
- **MUST** handle failures during temporary-file creation.
- **MUST** handle failures during file writing.
- **MUST** handle failures during file replacement.
- **MUST** handle failures during permission preservation.
- **MUST** handle failures during metadata preservation when metadata is intended to be preserved.
- **MUST NOT** assume that a file can always be replaced successfully after it has been read.
- **MUST** account for files being deleted, moved, or modified between discovery and processing.
- **MUST** detect or safely handle concurrent source modifications where necessary.
- **MUST NOT** silently overwrite a newer version of a file created by another process.
- **MUST** preserve executable permissions when replacing executable source files.
- **MUST** preserve relevant file permissions where replacing a file.
- **MUST** preserve the intended newline convention unless the tool explicitly normalizes it.
- **MUST** preserve the intended text encoding whenever possible.
- **MUST NOT** silently convert a file to a different encoding merely because the fixer rewrote it.
- **MUST** handle files containing UTF-8 and other supported encodings correctly.
- **MUST** fail clearly when a source file cannot be decoded safely.
- **MUST NOT** silently replace undecodable bytes with replacement characters.
- **MUST** handle a file with a UTF-8 BOM consistently.
- **MUST** preserve a BOM when the tool's source model and configuration require preservation.
- **MUST NOT** accidentally add or remove a BOM without an explicit and documented reason.

---

## 4. Parsing and Invalid Python

- **MUST** handle syntactically invalid Python without crashing the entire process.
- **MUST** report syntax errors using a useful diagnostic.
- **MUST** identify the relevant file and source location when possible.
- **MUST NOT** attempt unsafe AST-based fixes on source that cannot be parsed correctly.
- **MUST** distinguish parser failures from internal linter failures.
- **MUST NOT** silently skip a file because parsing failed.
- **MUST** provide a clear indication when a file could not be analyzed.
- **MUST** continue processing unrelated files when one file contains a recoverable parsing error, unless the configured mode explicitly requires fail-fast behavior.
- **MUST NOT** allow malformed input in one file to corrupt the analysis state of another file.
- **MUST** handle syntax introduced by supported Python versions correctly.
- **MUST NOT** parse code using a language version that is incompatible with the configured target without clearly reporting the mismatch.

---

## 5. Internal Errors and Failure Isolation

- **MUST NOT** crash silently.
- **MUST** return a non-success status when an internal error prevents reliable completion.
- **MUST** provide enough diagnostic information to identify the failed operation.
- **MUST** distinguish user errors from tool bugs where possible.
- **MUST NOT** hide internal errors behind a successful exit status.
- **MUST NOT** silently ignore unexpected exceptions during analysis or fixing.
- **MUST** prevent one broken rule from corrupting the results of unrelated rules whenever possible.
- **MUST** isolate rule failures from unrelated files and rules where practical.
- **MUST** report the affected rule and file when a rule implementation fails.
- **MUST** avoid producing a partially applied set of changes that is falsely presented as a complete successful run.
- **MUST** define whether fixes are committed per file, per batch, or per entire invocation.
- **MUST** ensure that the chosen commit boundary is safe and deterministic.
- **MUST NOT** leave users unable to determine which files were successfully modified after a partial failure.
- **MUST** preserve enough state to report partial completion accurately.
- **MUST** support useful diagnostics for unexpected internal failures without exposing sensitive information unnecessarily.

---

## 6. CLI Exit Codes

- **MUST** use exit codes consistently.
- **MUST** return a non-zero exit code when the configured check mode finds violations.
- **MUST** return a non-zero exit code when a fatal error prevents reliable analysis.
- **MUST** return a non-zero exit code when an auto-fix operation fails in a way that means the requested operation was not completed successfully.
- **MUST NOT** return success merely because the process did not crash.
- **MUST NOT** return success when files were silently skipped due to errors.
- **MUST** distinguish, where useful, between:

  - no violations;
  - violations found;
  - violations fixed;
  - invalid configuration;
  - invalid input;
  - internal tool failure.

- **MUST** make the exit-code contract stable and documented.
- **MUST NOT** make scripts depend on undocumented incidental exit-code behavior.

---

## 7. Diagnostics and User Feedback

- **MUST** report diagnostics in a machine-readable location format when the output format supports locations.
- **MUST** identify the file associated with each diagnostic.
- **MUST** report line and column information accurately when available.
- **MUST** report the rule identifier for every rule diagnostic.
- **MUST** provide a useful human-readable message.
- **MUST NOT** print misleading diagnostics after a fix has removed the underlying violation.
- **MUST** clearly distinguish diagnostics from warnings, errors, and informational messages.
- **MUST** clearly report files that could not be processed.
- **MUST** clearly report when a fix was not applied.
- **MUST** make warnings visible in normal operation when ignoring the condition could cause users to believe the tool succeeded incorrectly.
- **MUST NOT** silently downgrade a serious failure into a debug-only message.
- **MUST** provide an appropriate quiet mode where supported.
- **MUST NOT** let quiet mode suppress information required to understand a failed operation.
- **MUST** provide a machine-readable output mode suitable for CI and editor integrations.
- **MUST NOT** emit uncontrolled human-oriented text into a machine-readable output stream.
- **MUST** keep standard output and standard error semantics consistent.
- **MUST NOT** mix progress output into structured output without an explicit protocol.

---

## 8. Auto-Fix Modes

- **MUST** clearly distinguish check mode from fix mode.
- **MUST NOT** modify source files during a mode that promises not to modify files.
- **MUST** make the scope of auto-fixing explicit.
- **MUST** support a mode that reports available fixes without applying them when the interface promises such a mode.
- **MUST** make unsafe or unavailable fixes distinguishable from safe fixes.
- **MUST NOT** silently apply a fix when the user explicitly disabled fixes.
- **MUST** ensure that a fix-only operation does not accidentally alter unrelated files.
- **MUST** define how diagnostics that cannot be fixed are reported.
- **MUST** define how a partially fixable file is handled.
- **MUST NOT** claim that all violations were fixed when some remain.
- **MUST** ensure that the result of a fix run can be checked by a subsequent normal lint run.
- **MUST** make the relationship between fix mode and formatting mode explicit when both exist.
- **MUST NOT** allow independent tools or passes to continually undo each other's changes without detecting the conflict.

---

## 9. Determinism

- **MUST** produce deterministic diagnostics for identical input, configuration, environment, and tool version.
- **MUST** produce deterministic fixes for identical input, configuration, environment, and tool version.
- **MUST NOT** make output depend on nondeterministic filesystem traversal order unless explicitly documented.
- **MUST** define a deterministic ordering for diagnostics.
- **MUST** define a deterministic ordering for applying multiple fixes.
- **MUST NOT** allow parallel execution to change the final source output.
- **MUST NOT** allow hash-table, process, or thread scheduling order to affect the result.
- **MUST** ensure that cache hits and cache misses produce equivalent lint results.
- **MUST** ensure that running the tool twice on unchanged input produces the same result.
- **MUST** ensure that equivalent execution modes do not silently produce different fixes.
- **MUST** make environment-dependent behavior explicit when true determinism is impossible.

---

## 10. Idempotence

- **MUST** ensure that applying the same fix repeatedly converges to a stable result.
- **MUST NOT** produce an infinite fix loop.
- **MUST NOT** cause each invocation to make another cosmetic change to the same source indefinitely.
- **MUST** detect or prevent fix cycles between rules when possible.
- **MUST** define behavior when two rules intentionally transform the same construct in opposite directions.
- **MUST** ensure that a successful fix run does not produce new fixable violations indefinitely.
- **MUST** test fix idempotence explicitly.
- **MUST** ensure that the formatter and fixer do not continually fight each other when both are enabled.

---

## 11. Caching

- **MUST** ensure that a cache hit produces the same result as a fresh analysis of the same input.
- **MUST** invalidate cached results whenever any input affecting the result changes.
- **MUST** include the relevant tool version in the cache identity.
- **MUST** include the relevant rule configuration in the cache identity.
- **MUST** include the relevant target Python version in the cache identity.
- **MUST** include the relevant plugin or rule implementation version in the cache identity.
- **MUST** include all relevant configuration files in the cache identity.
- **MUST** include relevant environment-dependent inputs when the analysis depends on them.
- **MUST** include the source content or a reliable content identity in the cache key.
- **MUST NOT** reuse cached diagnostics for different source content.
- **MUST NOT** reuse cached results from an incompatible configuration.
- **MUST NOT** silently use stale cache data.
- **MUST** handle corrupted cache entries safely.
- **MUST** treat an unreadable cache entry as a cache miss or report a clear recoverable warning.
- **MUST NOT** crash because the cache is missing, corrupted, partially written, or inaccessible.
- **MUST** write cache entries atomically.
- **MUST NOT** leave partially written cache entries that can later be interpreted as valid.
- **MUST** tolerate cache directories being deleted between invocations.
- **MUST** tolerate cache directories being unavailable.
- **MUST** gracefully degrade to uncached execution when caching is unavailable.
- **MUST** warn when cache failures materially affect expected behavior or performance.
- **MUST NOT** make cache failure cause incorrect lint results.
- **MUST** prevent concurrent processes from corrupting shared cache state.
- **MUST** handle multiple tool versions using the same cache directory safely.
- **MUST** ensure that cache cleanup cannot delete unrelated user files.
- **MUST NOT** assume that a cache directory is writable merely because it exists.
- **MUST** ensure that disabling the cache truly disables cache reads and writes.
- **MUST** ensure that cache invalidation is based on actual relevant dependencies rather than arbitrary timeouts whenever correctness requires precise invalidation.
- **MUST NOT** prioritize cache performance over result correctness.

---

## 12. Incremental Execution

- **MUST** process only the requested scope.
- **MUST NOT** unexpectedly analyze or modify files outside the requested scope.
- **MUST** handle files added, deleted, renamed, or moved between discovery and processing.
- **MUST** avoid relying on stale file lists when the user explicitly requests a current filesystem state.
- **MUST** ensure that incremental results are equivalent to full results for the same effective input.
- **MUST NOT** produce different diagnostics merely because a file was analyzed incrementally rather than as part of a full run, unless cross-file analysis is explicitly approximate.
- **MUST** invalidate dependent results when a changed file affects cross-file analysis.
- **MUST** clearly document any limitations of incremental analysis.

---

## 13. Filesystem and Path Handling

- **MUST** handle paths containing spaces.
- **MUST** handle paths containing Unicode characters.
- **MUST** handle paths containing characters that are special to the shell.
- **MUST NOT** construct shell commands by unsafely interpolating user-controlled paths.
- **MUST** handle relative and absolute paths consistently.
- **MUST** define how symbolic links are handled.
- **MUST** avoid accidentally traversing unintended directories through symbolic links.
- **MUST** define behavior for broken symbolic links.
- **MUST** handle files disappearing during execution.
- **MUST** handle permission-denied files without crashing the entire run.
- **MUST** report inaccessible files clearly.
- **MUST NOT** silently skip inaccessible files.
- **MUST** handle filesystem case-sensitivity differences correctly.
- **MUST** avoid assuming that two paths with different spelling refer to different files.
- **MUST** avoid assuming that two paths with different spelling refer to the same file.
- **MUST** handle filesystems with unusual timestamp resolution safely.
- **MUST NOT** rely solely on timestamps when doing so can produce stale or incorrect results.
- **MUST** handle network filesystems as gracefully as possible.
- **MUST NOT** assume that filesystem operations are instantaneous or atomic beyond the guarantees actually provided by the platform.

---

## 14. Cross-Platform Behavior

- **MUST** support all officially supported operating systems.
- **MUST** either implement platform-specific behavior correctly or gracefully degrade with a clear warning.
- **MUST NOT** hard-crash merely because an optional platform feature is unavailable.
- **MUST NOT** silently enter a broken state because an operating-system feature differs.
- **MUST** handle Windows path semantics correctly when Windows is supported.
- **MUST** handle POSIX path semantics correctly when POSIX systems are supported.
- **MUST** handle platform-specific path separators correctly.
- **MUST** handle platform-specific path normalization correctly.
- **MUST** handle platform-specific file replacement behavior correctly.
- **MUST** handle platform-specific file locking behavior correctly.
- **MUST** handle platform-specific permission semantics correctly.
- **MUST** handle platform-specific newline behavior correctly.
- **MUST** handle platform-specific process and signal behavior correctly.
- **MUST** not assume that POSIX signals exist or behave identically on Windows.
- **MUST** not assume that Unix-specific filesystem features exist on Windows.
- **MUST** not assume that Windows-specific filesystem behavior exists on POSIX.
- **MUST** provide a reasonable fallback when optional acceleration is unavailable.
- **MUST** ensure that unsupported optimizations do not become correctness requirements.
- **MUST** test supported platforms in CI or through an equivalent reliable validation process.
- **MUST NOT** claim cross-platform support if core functionality is untested on a supported platform.

---

## 15. Process, Signals, and Cancellation

- **MUST** handle user cancellation gracefully where the operating system provides cancellation mechanisms.
- **MUST** avoid leaving files partially modified after cancellation.
- **MUST** define whether cancellation stops immediately or after completing the current safe operation.
- **MUST** ensure that cancellation does not corrupt shared cache state.
- **MUST** ensure that cancellation does not leave stale locks indefinitely.
- **MUST** clean up temporary files where possible.
- **MUST** handle termination during file replacement safely.
- **MUST** avoid treating every interruption as a successful completion.
- **MUST** return an appropriate non-success status when the requested operation was interrupted.
- **MUST** ensure that child processes do not remain unexpectedly orphaned when the tool is terminated.
- **MUST** handle environments where normal signal behavior is restricted or unavailable.
- **MUST** degrade gracefully when optional signal functionality is unavailable.

---

## 16. Parallelism and Concurrency

- **MUST** ensure that parallel execution produces the same logical result as serial execution.
- **MUST** ensure that parallel workers cannot corrupt one another's source files.
- **MUST** ensure that parallel workers cannot corrupt shared cache state.
- **MUST** define ownership of each file being modified.
- **MUST NOT** allow two workers to apply competing modifications to the same file without coordination.
- **MUST** make diagnostic ordering deterministic despite parallel execution.
- **MUST** handle worker crashes without silently losing work.
- **MUST** report worker failures accurately.
- **MUST** avoid deadlocks.
- **MUST** avoid indefinite waits for failed workers.
- **MUST** clean up workers after failure or cancellation.
- **MUST** avoid unbounded memory growth as the number of files or diagnostics increases.
- **MUST** respect configured resource limits where such limits exist.
- **MUST** handle environments where multiprocessing is restricted or unavailable.
- **MUST** gracefully fall back to serial execution when parallel execution cannot be used safely.
- **MUST NOT** silently produce incomplete results because a worker failed.

---

## 17. Configuration

- **MUST** validate configuration before performing potentially destructive operations.
- **MUST** report invalid configuration clearly.
- **MUST** identify the configuration source and location when possible.
- **MUST NOT** silently ignore misspelled configuration keys unless that behavior is explicitly documented.
- **MUST NOT** silently accept invalid configuration values.
- **MUST** define configuration precedence deterministically.
- **MUST** make command-line overrides behave consistently.
- **MUST** ensure that configuration changes invalidate relevant cached results.
- **MUST** ensure that configuration from the intended project is used.
- **MUST NOT** accidentally apply configuration from an unrelated parent directory.
- **MUST** define behavior when multiple configuration files are present.
- **MUST** define behavior when configuration files contain unsupported versions or schemas.
- **MUST** provide useful errors for invalid rule selectors.
- **MUST** make rule enablement and disablement predictable.
- **MUST NOT** allow an accidentally broad configuration to silently enable destructive behavior without the user requesting it.
- **MUST** document defaults that materially affect analysis or fixing.
- **MUST** ensure that default behavior is stable enough for automation.

---

## 18. Rule and Plugin Isolation

- **MUST** ensure that a rule cannot accidentally modify source outside its declared scope.
- **MUST** define the interface between rules and the core engine.
- **MUST** validate rule-provided edits before applying them.
- **MUST** handle malformed rule output safely.
- **MUST** isolate failures in third-party or dynamically loaded rules where practical.
- **MUST** identify the rule responsible for a failed diagnostic or fix.
- **MUST NOT** allow a broken plugin to corrupt the core linter state.
- **MUST NOT** allow a plugin to silently bypass global safety checks.
- **MUST** apply global validation to all fixes regardless of their source.
- **MUST** define compatibility requirements for plugins.
- **MUST** invalidate caches when rule implementation behavior changes.
- **MUST** define how duplicate rule identifiers are handled.
- **MUST NOT** silently resolve conflicting rule identifiers in an ambiguous way.
- **MUST** ensure that disabled rules do not produce diagnostics or fixes.
- **MUST** ensure that rule selection is deterministic.

---

## 19. Python Version and Language Compatibility

- **MUST** define the supported Python language versions.
- **MUST** analyze syntax according to the configured target version.
- **MUST** reject or clearly report unsupported syntax.
- **MUST NOT** silently interpret newer syntax as older syntax incorrectly.
- **MUST** ensure that auto-fixes are valid for the configured target version.
- **MUST NOT** apply a fix that introduces syntax unsupported by the target Python version.
- **MUST** ensure that version-dependent rules use the configured target version consistently.
- **MUST** include the target version in relevant cache identity.
- **MUST** test rules across all supported language versions where behavior differs.
- **MUST** distinguish the Python version used to run the linter from the Python version being analyzed.

---

## 20. Parsing, AST, CST, and Source Mapping

- **MUST** maintain accurate mappings between analyzed syntax and source locations.
- **MUST** handle source offsets consistently.
- **MUST** define whether offsets are measured in bytes, Unicode code points, or another unit.
- **MUST NOT** mix incompatible offset units.
- **MUST** handle multibyte Unicode characters correctly.
- **MUST** handle tabs correctly when calculating displayed columns.
- **MUST** preserve source ranges accurately when applying edits.
- **MUST** ensure that source transformations do not invalidate subsequent edit locations unexpectedly.
- **MUST** apply multiple edits in a way that preserves their intended source ranges.
- **MUST** handle nested and adjacent edits deterministically.
- **MUST** reject invalid edit ranges safely.
- **MUST NOT** allow an invalid edit range to cause memory corruption, arbitrary file corruption, or an uncontrolled crash.

---

## 21. Formatting and Source Preservation

- **MUST** define which parts of formatting the linter owns.
- **MUST NOT** unexpectedly reformat unrelated code.
- **MUST** preserve formatting outside the intended edit range when the tool is not a formatter.
- **MUST** preserve comments where possible.
- **MUST** preserve blank-line structure where the fix does not own it.
- **MUST** preserve quote style where the fix does not require changing it.
- **MUST** avoid unnecessary whole-file rewrites.
- **MUST** minimize diffs produced by an auto-fix.
- **MUST NOT** rewrite an entire file merely to apply a small local fix unless the architecture explicitly requires and documents that behavior.
- **MUST** ensure that formatting changes are deterministic.
- **MUST** avoid creating formatting churn across repeated runs.

---

## 22. Git and VCS Integration

- **MUST** avoid modifying files outside the requested scope.
- **MUST** define behavior when the working tree contains uncommitted changes.
- **MUST NOT** silently discard user changes.
- **MUST NOT** reset or overwrite unrelated user work.
- **MUST** handle files modified after the linter discovered them.
- **MUST** provide a safe behavior when a file changed concurrently.
- **MUST** define whether ignored files are analyzed.
- **MUST** define how VCS ignore rules are handled.
- **MUST** not assume that Git is installed unless Git integration is explicitly required.
- **MUST** gracefully degrade when optional VCS integration is unavailable.
- **MUST** handle repositories with unusual paths and worktree layouts correctly.
- **MUST NOT** execute arbitrary repository-controlled commands merely to determine lint scope unless explicitly required and safely constrained.
- **MUST** treat repository configuration as potentially untrusted when the tool may run in untrusted repositories.
- **MUST** ensure that VCS integration cannot silently change source files beyond the requested operation.

---

## 23. Security

- **MUST** treat source code and repository contents as potentially untrusted input.
- **MUST** avoid executing source code during normal static analysis unless explicitly required.
- **MUST** avoid importing analyzed project modules merely to inspect them unless explicitly required.
- **MUST** avoid executing arbitrary project configuration code unless the behavior is explicit and trusted.
- **MUST** avoid shell injection through filenames, configuration values, or source content.
- **MUST** avoid path traversal when creating temporary or cache files.
- **MUST** ensure that temporary files are created safely.
- **MUST** avoid predictable temporary-file names where attackers could exploit them.
- **MUST** protect cache integrity where cache poisoning could affect correctness.
- **MUST** avoid following untrusted paths outside the intended scope unless explicitly configured.
- **MUST** avoid exposing sensitive source content in diagnostics unnecessarily.
- **MUST** avoid including secrets in cache keys, logs, or error messages.
- **MUST** ensure that diagnostic output cannot be interpreted as executable shell input without appropriate escaping.
- **MUST NOT** assume that a repository is trusted merely because it is local.
- **MUST** document any behavior that executes external programs or project-defined code.

---

## 24. Resource Usage

- **MUST** have bounded or controllable memory usage for large projects.
- **MUST** avoid loading an unbounded number of entire files into memory unnecessarily.
- **MUST** avoid unbounded diagnostic accumulation where output can be streamed or bounded.
- **MUST** avoid unbounded cache growth.
- **MUST** avoid unbounded temporary-file accumulation.
- **MUST** provide reasonable behavior for very large files.
- **MUST** define behavior when a file exceeds supported size limits.
- **MUST** fail clearly rather than exhausting system resources unpredictably.
- **MUST** avoid quadratic or worse behavior on common large inputs unless explicitly justified.
- **MUST** avoid pathological behavior caused by deeply nested source code where possible.
- **MUST** handle recursion limits safely.
- **MUST** ensure that a malicious or pathological input cannot trivially cause uncontrolled resource exhaustion if the tool is intended to process untrusted input.

---

## 25. Timeouts and Hanging Operations

- **MUST** avoid indefinite hangs.
- **MUST** define timeout behavior for operations that can block indefinitely.
- **MUST** ensure that a timeout does not leave source files in a corrupted state.
- **MUST** ensure that a timeout does not silently report success.
- **MUST** report which operation timed out where possible.
- **MUST** clean up resources after a timeout.
- **MUST** handle blocked filesystem operations as gracefully as the platform permits.
- **MUST** handle worker processes that stop responding.
- **MUST** ensure that timeout mechanisms behave correctly in supported execution environments.
- **MUST NOT** assume that a terminal, TTY, process group, or signal behaves identically in local shells, CI, containers, IDEs, or remote execution environments.
- **MUST** gracefully degrade when optional timeout or process-control functionality is unavailable.
- **MUST NOT** leave the user with an apparently hung process and no way to determine what is happening.

---

## 26. Standard Input, Output, and TTY Behavior

- **MUST** define behavior when reading source from standard input.
- **MUST** define behavior when standard input is not a TTY.
- **MUST** define behavior when standard output is not a TTY.
- **MUST** define behavior when standard error is not a TTY.
- **MUST** work correctly when invoked from CI.
- **MUST** work correctly when invoked through pipes.
- **MUST** work correctly when invoked from an IDE or editor integration where terminal capabilities may be limited.
- **MUST** work correctly when invoked through a process supervisor.
- **MUST NOT** require an interactive TTY for ordinary non-interactive operation unless explicitly documented.
- **MUST NOT** hang merely because stdin or stdout is redirected.
- **MUST** avoid terminal-control operations when no compatible terminal is available.
- **MUST** gracefully degrade when optional terminal features are unavailable.
- **MUST NOT** assume that a process is attached to the terminal's foreground process group.
- **MUST** ensure that output remains usable when terminal capabilities are unavailable.

---

## 27. Logging and Debugging

- **MUST** provide sufficient diagnostic information to investigate failures.
- **MUST** provide a debug or verbose mode when normal output is insufficient for troubleshooting.
- **MUST NOT** require debug logging for ordinary users to understand a normal lint failure.
- **MUST** ensure that debug logging does not change lint results.
- **MUST** ensure that debug logging does not change auto-fix behavior.
- **MUST NOT** log secrets or sensitive source content unnecessarily.
- **MUST** ensure that log output does not corrupt structured output modes.
- **MUST** make it possible to identify the relevant file, rule, and operation during internal failures.
- **MUST** avoid logging enormous source contents by default.
- **MUST** provide enough context to distinguish a tool bug from an invalid user input.

---

## 28. Configuration and Environment Discovery

- **MUST** define how the project root is discovered.
- **MUST** define how configuration files are discovered.
- **MUST** define how environment variables affect behavior.
- **MUST** make environment-dependent behavior explicit.
- **MUST NOT** silently use a configuration file from an unexpected directory.
- **MUST** handle missing configuration files gracefully.
- **MUST** handle malformed configuration files clearly.
- **MUST** define precedence between command-line arguments, environment variables, configuration files, and defaults.
- **MUST** ensure that the same invocation produces predictable results from different working directories when the selected project is the same.
- **MUST** avoid accidentally depending on the caller's current working directory when the intended project root is known.
- **MUST** ensure that environment variables cannot silently override explicit command-line configuration unless documented.

---

## 29. Installation and Runtime Environment

- **MUST** provide a clear error when required runtime dependencies are unavailable.
- **MUST** avoid silently using an incompatible dependency version.
- **MUST** handle missing optional dependencies gracefully.
- **MUST** clearly distinguish optional-feature failure from core linter failure.
- **MUST** support the officially supported Python runtime versions.
- **MUST** report incompatible runtime versions clearly.
- **MUST NOT** silently fall back to an incompatible implementation that changes correctness.
- **MUST** ensure that installed plugins are compatible with the core linter version.
- **MUST** define behavior when multiple versions of a plugin or dependency are present.

---

## 30. Performance

- **MUST** provide acceptable performance on typical projects.
- **MUST** avoid performing expensive work that cannot affect the result.
- **MUST** avoid re-reading or re-parsing unchanged files unnecessarily when caching is enabled.
- **MUST** ensure that caching improves performance without changing correctness.
- **MUST** ensure that parallel execution does not introduce disproportionate overhead for small projects.
- **MUST** avoid making startup overhead dominate execution for small inputs without good reason.
- **MUST** avoid pathological performance on common Python constructs.
- **MUST** measure performance regressions where performance is a project requirement.
- **MUST NOT** sacrifice correctness merely to improve benchmark results.
- **MUST** ensure that performance optimizations have equivalent observable semantics.

---

## 31. Testing Requirements

- **MUST** test every auto-fixer with valid input.
- **MUST** test every auto-fixer with invalid or unsupported input where relevant.
- **MUST** test that every fixer produces syntactically valid Python.
- **MUST** test that fixers preserve intended source semantics.
- **MUST** test fix idempotence.
- **MUST** test conflicting fixes.
- **MUST** test overlapping fixes.
- **MUST** test multiple fixes in the same file.
- **MUST** test files with comments.
- **MUST** test files with Unicode.
- **MUST** test files with different newline conventions where supported.
- **MUST** test files with encoding declarations where supported.
- **MUST** test empty files.
- **MUST** test very small files.
- **MUST** test large files.
- **MUST** test malformed Python.
- **MUST** test missing files.
- **MUST** test inaccessible files.
- **MUST** test files being changed or deleted during execution where practical.
- **MUST** test cache hits.
- **MUST** test cache misses.
- **MUST** test cache invalidation.
- **MUST** test corrupted cache entries.
- **MUST** test unavailable cache directories.
- **MUST** test concurrent cache access.
- **MUST** test serial and parallel execution.
- **MUST** test cancellation.
- **MUST** test internal rule failures.
- **MUST** test invalid configuration.
- **MUST** test all officially supported operating systems.
- **MUST** test non-TTY execution.
- **MUST** test CI-like execution environments.
- **MUST** test paths containing spaces and Unicode.
- **MUST** test the documented exit-code contract.
- **MUST** include regression tests for every previously discovered correctness bug.
- **MUST NOT** consider a test that merely checks that the process did not crash sufficient evidence of correctness.

---

## 32. Testing the Auto-Fix Pipeline

- **MUST** test the complete pipeline, not only individual edit-generation functions.
- **MUST** test:

  1. source discovery;
  2. reading;
  3. parsing;
  4. diagnostic generation;
  5. fix generation;
  6. fix conflict resolution;
  7. source reconstruction;
  8. validation;
  9. file replacement;
  10. post-fix verification.

- **MUST** test failure at every significant stage.
- **MUST** verify that failures do not corrupt the source file.
- **MUST** verify that the reported result matches the actual final file state.
- **MUST** test interruption at safe and unsafe points in the pipeline.
- **MUST** test partial failure behavior explicitly.

---

## 33. Compatibility and Upgrade Behavior

- **MUST** define how behavior changes between tool versions.
- **MUST** invalidate caches when a version change can affect results.
- **MUST** avoid silently changing auto-fix semantics in a patch release unless the compatibility policy permits it.
- **MUST** document behavior changes that can produce large source diffs.
- **MUST** provide a migration path for incompatible configuration changes.
- **MUST** avoid silently interpreting old configuration according to a materially different meaning.
- **MUST** ensure that serialized internal data is versioned or safely invalidated when its format changes.
- **MUST** ensure that old cache data cannot silently produce incorrect results after an incompatible upgrade.

---

## 34. User Trust

- **MUST** do what the command claims to do.
- **MUST NOT** silently modify files outside the requested scope.
- **MUST NOT** silently discard user changes.
- **MUST NOT** silently ignore failures.
- **MUST NOT** silently use stale analysis results when they can affect correctness.
- **MUST NOT** silently apply unsafe transformations.
- **MUST** make destructive behavior explicit.
- **MUST** make incomplete execution visible.
- **MUST** make errors actionable.
- **MUST** make automated execution reliable.
- **MUST** ensure that running the linter repeatedly does not create unexplained source churn.
- **MUST** ensure that CI can reliably determine whether the code is compliant.
- **MUST** ensure that editor integrations can reliably determine the current state of diagnostics.
- **MUST** prefer a visible failure over a silent incorrect result.
- **MUST** prefer leaving the original source unchanged over corrupting or ambiguously modifying it.

---

## 35. General Failure Principle

The linter **MUST** follow this general rule:

> When the tool cannot confidently complete an operation correctly, it must fail visibly and preserve user data whenever possible.

The linter **MUST NOT** follow this rule:

> Try something, ignore errors, and report success if the process did not crash.

For every operation that can modify source code, the implementation should be answerable to the following questions:

1. **What are the inputs?**
2. **What can fail?**
3. **Can the failure corrupt user data?**
4. **Can the failure produce an incorrect lint result?**
5. **Can the failure be detected?**
6. **What is the safe fallback?**
7. **Will the user know that the operation did not complete successfully?**
8. **Will a later run produce the same result?**
9. **Can the operation be safely interrupted?**
10. **Can the result be independently validated?**

A robust auto-fixing linter should generally prefer:

- **no change over an unsafe change;**
- **a visible warning over silent degradation;**
- **a cache miss over stale data;**
- **serial execution over incorrect parallel execution;**
- **a clear error over a misleading success;**
- **preserving the original file over attempting a risky recovery;**
- **a small, reviewable diff over an unnecessary whole-file rewrite.**
