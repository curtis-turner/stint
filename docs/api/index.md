# API reference

The public API surface is re-exported from `stint.__init__`; import from the top-level package (`from stint import CustomField, create_engine, select`) or the subpackage that owns it.

| Module | Contains |
|---|---|
| [Engine](engine.md) | `Engine`, `TmpEngine`, `create_engine`, `create_tmp_engine`, dialect resolution |
| [Schema plane](schema.md) | `IssueType`, `Project` base classes and the declaration dataclasses |
| [Fields](fields.md) | `CustomField` and the 18 field-type classes |
| [Query plane](query.md) | `Session`, `AsyncSession`, `select`, `Select`, `Column`, combinators, ADF helpers |
| [Migrations](migrations.md) | `Migration`, revision graph, the `op` namespace, `get_context`, `load_migrations` |
| [Autogeneration](autogen.md) | Desired-state builders, diffing, migration emission, schema loading, stamping |
| [State](state.md) | `StateFile` and the snapshot types |
| [Registry](registry.md) | The alias registry and its singleton |
| [Validation](validate.md) | `validate`, `validate_or_raise` |
| [Client](client.md) | HTTP client and auth primitives |
| [Dialects](dialects.md) | The dialect protocol and the Jira Cloud implementations |
| [Exceptions](exceptions.md) | The `StintError` hierarchy |

Most entries below are rendered straight from the package docstrings by mkdocstrings.
