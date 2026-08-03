"""SQLAlchemy metadata for a Prisma schema.

```python
from prisma import sa

md = sa.metadata()  # a sqlalchemy.MetaData
accounts = sa.table_for('Account')  # a sqlalchemy.Table
```

`metadata()` describes the same database `prisma db push` creates — same tables,
columns, types, constraint names, foreign key actions and implicit
many-to-many join tables. Point Alembic's `target_metadata` at it and
autogenerate produces an empty diff against a Prisma-managed database; that
equivalence is the test this package is held to.

Requires a client generated with `schemaMetadata = true`, and PostgreSQL — see
`prisma.sa._types` for why the verified provider list is short.

Nothing here executes queries. It is the schema half of the SQLAlchemy
migration: it lets you run Alembic, reflect, and write SQLAlchemy Core queries
against the tables your Prisma client is already using, before anything about
how queries execute changes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from .._schema import get_schema, get_provider, get_enum_schema

if TYPE_CHECKING:
    import sqlalchemy as sa

    # Served at runtime by the module `__getattr__` below, so that reaching for
    # `prisma.sa` does not import SQLAlchemy until something actually needs it.
    # Declared here as well or a type checker cannot see the names `__all__`
    # promises. Ruff reads that as a misplaced runtime import; it is not.
    from ._build import build_metadata as build_metadata  # noqa: TCH004
    from ._types import UnsupportedProviderError as UnsupportedProviderError  # noqa: TCH004

#: Version of the `prisma.sa` contract, independent of the client version.
#: Both this fork and upstream report `prisma.__version__ == '0.15.0'`, so there
#: is otherwise no way to tell them apart, or to tell which set of fixes a given
#: checkout carries. Bump it whenever the emitted schema changes.
__version__ = '1.2.0'

__all__ = (
    '__version__',
    'metadata',
    'table_for',
    'join_table_for',
    'build_metadata',
    'clear_cache',
    'UnsupportedProviderError',
)

_cache: Optional[Any] = None


def __getattr__(name: str) -> Any:
    # Imported lazily so that `import prisma` does not pull in SQLAlchemy for
    # the majority of users who are not using it.
    if name == 'build_metadata':
        from ._build import build_metadata

        return build_metadata
    if name == 'UnsupportedProviderError':
        from ._types import UnsupportedProviderError

        return UnsupportedProviderError
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')


def metadata() -> 'sa.MetaData':
    """The `MetaData` for the generated schema, built once and cached."""
    global _cache

    if _cache is None:
        from ._build import build_metadata

        _cache = build_metadata(get_schema(), get_enum_schema(), get_provider())

    return _cache


def clear_cache() -> None:
    """Drop the cached `MetaData`.

    Only useful in tests, and after regenerating the client in-process.
    """
    global _cache
    _cache = None


def table_for(model: str) -> 'sa.Table':
    """The table a Prisma model maps to, e.g. `table_for('Account')`.

    Takes the *model* name, not the table name — `@@map` is exactly what this
    resolves.
    """
    schema = get_schema()
    try:
        spec = schema[model]
    except KeyError:
        raise LookupError(f'Unknown model: {model}') from None

    return metadata().tables[_qualified(spec['table'])]


def join_table_for(model: str, field: str) -> 'sa.Table':
    """The join table behind an implicit many-to-many relation.

    Prisma manages this table invisibly — it has no model, so `table_for` cannot
    reach it, and a query that traverses the relation needs it.
    """
    from .._schema import relation

    rel = relation(model, field)
    if rel['shape'] != 'many-to-many':
        raise LookupError(
            f'{model}.{field} is a {rel["shape"]} relation; only implicit many-to-many relations have a join table'
        )

    # `.get`, not `[...]`: the join keys are only emitted for a many-to-many
    # relation, so a missing one here means the metadata disagrees with its own
    # `shape` — worth a clear error rather than a KeyError from deep inside.
    join_table = rel.get('join_table')
    if join_table is None:
        raise LookupError(f'{model}.{field} is many-to-many but carries no join table name')

    return metadata().tables[_qualified(join_table)]


def _qualified(table: str) -> str:
    md = metadata()
    return f'{md.schema}.{table}' if md.schema else table
