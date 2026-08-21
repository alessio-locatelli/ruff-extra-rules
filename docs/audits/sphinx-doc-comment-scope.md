# Research: Sphinx `#:` documentation-comment scope

## Finding

Sphinx `autodoc` defines `#:` as a documentation comment for module data members and class attributes. It recognises a one-line comment on the assignment or one or more contiguous lines before it. The published class example also recognises `self` assignments in `__init__` as instance attributes. Ordinary function-local assignments are not a documented target.

The implementation confirms that distinction. Its parser records assignments in module and class context, and records assignments inside `__init__` under the containing class; it returns no qualified name for other function-local assignments. It handles both `ast.Assign` and `ast.AnnAssign`, and also `ast.TypeAlias` in current Sphinx.

Therefore, excluding every comment that starts `#:` is the safe rule for a formatter that must never rewrite a Sphinx documentation comment. A narrower exemption limited to module-level assignments remains vulnerable to documented class and `__init__` attribute comments. This is intentionally conservative: Sphinx's current parser skips `try` handlers and `finally` suites, but leaving a possible documentation comment unchanged is safer than making a destructive inference about a user's documentation toolchain.

## Sources

- [Sphinx autodoc: doc comments and docstrings](https://www.sphinx-doc.org/en/master/usage/extensions/autodoc.html#doc-comments-and-docstrings) defines `#:` comments, their permitted placement, and shows class and `__init__` instance attributes.
- [Sphinx autodoc: automatic data and attributes](https://www.sphinx-doc.org/en/master/usage/extensions/autodoc.html#automatically-document-attributes-or-data) defines `autodata` for module variables/constants and `autoattribute` for class attributes.
- [Sphinx parser scope selection](https://github.com/sphinx-doc/sphinx/blob/master/sphinx/pycode/parser.py#L1854-L1870) rejects ordinary function locals while retaining class `__init__` instance attributes.
- [Sphinx parser assignment comment collection](https://github.com/sphinx-doc/sphinx/blob/master/sphinx/pycode/parser.py#L1978-L2085) processes `Assign` and `AnnAssign`; [PEP 695 aliases](https://github.com/sphinx-doc/sphinx/blob/master/sphinx/pycode/parser.py#L2254-L2266) are processed too.
- [Sphinx parser try traversal](https://github.com/sphinx-doc/sphinx/blob/master/sphinx/pycode/parser.py#L2181-L2195) visits only a `try` body's and `else` suite, not its handlers or `finally` suite.
- [ModuleAnalyzer](https://github.com/sphinx-doc/sphinx/blob/master/sphinx/pycode/__init__.py#L799-L848) exposes those parsed comments as class and module attribute documentation.
