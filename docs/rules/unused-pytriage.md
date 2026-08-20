# unused-pytriage (TR8)

Reports `# pytriage` codes that no longer suppress a matching violation from an active check.

Select it alongside the checks you want to audit:

```python
result = 42  # pytriage: TR1
```

If `meaningless-vars` is active and does not report this line, `unused-pytriage` reports the redundant `TR1` entry. Codes are evaluated independently, so a valid entry in a comma-separated list remains untouched. Unknown codes are retained because they may belong to another or future check.

This check only reports; it never changes files, including when `--fix` is used.
