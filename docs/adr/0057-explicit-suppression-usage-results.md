# Preserve suppression usage as explicit check-result metadata

`unused-pytriage` needs to distinguish an inline comment that suppressed a real candidate from one that no longer does anything, but existing checks discard suppressed candidates before the orchestrator sees them. Checks therefore expose suppression-usage records as explicit per-file result metadata, which the orchestrator caches and passes to the opt-in audit; the design avoids hidden shared ledgers and shared token streams, preserves each check's own suppression semantics, and lets the audit recompute against the final source after fixes.

The audit uses only checks that actually ran for the file, ignores unknown codes and format-suppressed comments, reports duplicate entries independently, and never lets `TR8` self-suppress an unused entry.
