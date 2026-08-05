# A check declares each option once, as data

A check used to register its own configuration by implementing two classmethods: one adding arguments to the shared `argparse` parser, one translating the parsed namespace back into its own constructor keywords. That worked while the command line was the only source of configuration.

Adding `pyproject.toml` (ADR-0045) needs the same facts again — the key name, the accepted values, the default — plus two the CLI never needed: the list of valid keys, so an unknown one can be rejected with the valid alternatives named, and a validator that can report where a bad value came from. Declaring all of that a second time per check would put the two descriptions of one option in different places, free to drift: a value added to one and not the other is a silent bug, and this project's own guidelines rule out that duplication.

## Decision

A check declares its options once, as data, in a class-level `OPTIONS` tuple. The command-line flag, the TOML key, the accepted values, the validation and its error message, the valid-key list, and the constructor keyword are all derived from that one declaration. The two classmethods are removed rather than kept alongside it, so there is exactly one place an option can be declared.

The option name is the constructor keyword. The flag is the check id joined to it (`--meaningless-vars-level`); the TOML key is the bare name inside the check's own sub-table. A check with nothing to configure declares nothing.

Derived flags default to `None` rather than to the option's real default. Without that, a flag left unset is indistinguishable from one explicitly given its default value, and `argparse`'s own default would outrank the `pyproject.toml` value it is supposed to lose to — silently inverting the precedence order. The same requirement makes `--fix` a `None`-defaulted flag paired with `--no-fix`.

`OPTIONS` is part of the `ASTCheck` protocol, so every check — including test doubles — declares it rather than the CLI defending against its absence.

## Considered Options

- **Keep the two classmethods and add config-file counterparts beside them**: rejected. Smaller diff, but it is exactly the duplication described above.
- **Derive the TOML schema by introspecting the built `argparse` parser**: rejected. One declaration, but it makes `argparse`'s internals the configuration schema, so any change to how flags are registered silently changes the file format.

## Consequences

- Adding a configurable option to a check is one declaration; adding a new _kind_ of option (something other than a fixed set of named values) requires a new descriptor type, which nothing needs yet.
- Every option is reachable from both sources by construction. That is what makes `ruff`'s inline `--config "key = value"` form unnecessary here (ADR-0045).
- Help text is now shared between `--help` and configuration error messages, so it has to read sensibly in both.
