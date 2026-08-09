# PyMongo document rename provenance

## Context

The generic producer rule derived `one` and `one_and_delete` from `find_one()` and related methods. Those words describe the method shape, not the returned value, so they were not useful variable names. PyMongo operations in this family return a single document, but treating every method with that spelling as PyMongo would misidentify unrelated APIs.

## Decision

ADR-0038's rejection of PyMongo-aware provenance tracking is superseded for the single-document collection methods. TR1 recognizes a PyMongo collection only from file-local evidence: supported static PyMongo imports, an annotated or constructed `MongoClient`, and collection construction through indexed client access or `get_database(...).get_collection(...)`. It follows those bindings through local names and instance attributes.

For a proven collection, `find_one`, `find_one_and_delete`, `find_one_and_replace`, and `find_one_and_update` produce the auto-fixable name `document`. A concrete assignment annotation takes precedence, including a nullable annotation with one concrete type. Without either source of evidence, the generic producer rule must not suggest the uninformative method tails `one` or `one_and_*`.

Existing bare DB-API method mappings remain suggestion-only because they do not establish the receiver type.

## Consequences

- PyMongo code receives a useful automatic rename without resolving installed type stubs or inspecting other files.
- Unknown `find_one*` APIs remain silent instead of receiving a mechanically derived name.
- The analysis stays deterministic, offline, and bounded to the parsed file.
