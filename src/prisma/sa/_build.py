"""Turn the generated schema metadata into a SQLAlchemy `MetaData`.

This is deliberately built at runtime from `metadata.SCHEMA` rather than emitted
as declarative source by a template. One implementation, tested once against a
real database, beats a template whose output has to be re-checked for every
schema shape — and the query compiler wants `Table` objects (SQLAlchemy Core),
not declarative classes, anyway. Dumping declarative source for people who want
it checked in is planned as `prisma py sqlalchemy generate`, and does not exist
yet.

The correctness bar is an **empty Alembic autogenerate diff** against a database
built by `prisma db push`, plus direct assertions on the things autogenerate
does not compare: foreign key actions, constraint names, index methods and
server defaults.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence

import sqlalchemy as sa

from ._types import enum_type, array_type, scalar_type, check_provider

__all__ = ('build_metadata', 'JOIN_LEFT', 'JOIN_RIGHT')

#: Column names Prisma gives the two sides of an implicit m2m join table.
JOIN_LEFT = 'A'
JOIN_RIGHT = 'B'

#: Prisma default generators that produce the value **client-side**. They must
#: not become a server default: adding one is a schema diff on every migration,
#: and worse, it hides a missing client-side value behind a working INSERT.
_CLIENT_SIDE_GENERATORS = frozenset({'cuid', 'uuid', 'nanoid', 'ulid', 'auto'})

_REFERENTIAL_ACTIONS = {
    'Cascade': 'CASCADE',
    'Restrict': 'RESTRICT',
    'NoAction': 'NO ACTION',
    'SetNull': 'SET NULL',
    'SetDefault': 'SET DEFAULT',
}


def build_metadata(
    schema: Mapping[str, Any],
    enums: Mapping[str, Any],
    provider: str,
    *,
    metadata: Optional[sa.MetaData] = None,
) -> sa.MetaData:
    """Build a `MetaData` describing the same database Prisma would create.

    `schema` and `enums` are `metadata.SCHEMA` / `metadata.ENUM_SCHEMA` from a
    client generated with `schemaMetadata = true`.
    """
    check_provider(provider)
    md = metadata if metadata is not None else sa.MetaData()

    enum_types = {
        name: enum_type(provider, spec['db_name'], list(spec['values'].values()), md) for name, spec in enums.items()
    }

    for spec in schema.values():
        _build_table(md, spec, provider, enum_types)

    # Foreign keys go in a second pass: a relation can point at a model that has
    # not been built yet, and a self-relation points at the table being built.
    for spec in schema.values():
        _add_foreign_keys(md, schema, spec)

    # Both sides of an implicit m2m describe the same join table, so build from
    # whichever is seen first and skip the mirror.
    built: set[str] = set()
    for model, spec in schema.items():
        for rel in spec['relations'].values():
            if rel['shape'] != 'many-to-many':
                continue
            if rel['join_table'] in built:
                continue
            built.add(rel['join_table'])
            _build_join_table(md, schema, rel, model)

    return md


def _build_table(
    md: sa.MetaData,
    spec: Mapping[str, Any],
    provider: str,
    enum_types: Mapping[str, Any],
) -> sa.Table:
    primary_key = set(spec['primary_key']['fields'])
    args: List[Any] = []

    for name, field in spec['fields'].items():
        args.append(_build_column(field, provider, enum_types, is_primary_key=name in primary_key))

    if spec['primary_key']['db_name'] is not None:
        # `@@id(map: "...")`. Left to the dialect otherwise, which produces the
        # same `<table>_pkey` Prisma does on PostgreSQL.
        args.append(
            sa.PrimaryKeyConstraint(
                *spec['primary_key']['columns'],
                name=spec['primary_key']['db_name'],
            )
        )

    table = sa.Table(spec['table'], md, *args)

    # A unique *index*, not a `UniqueConstraint`. Verified against `prisma db
    # push`: Prisma emits `CREATE UNIQUE INDEX` even on PostgreSQL, where a
    # unique constraint would be the more idiomatic choice. Alembic tells the
    # two apart, so a constraint here means a drop-and-recreate on every
    # migration.
    for unique in spec['uniques']:
        sa.Index(unique['db_name'], *[table.c[column] for column in unique['columns']], unique=True)

    for index in spec['indexes']:
        kwargs: Dict[str, Any] = {}
        if index['algorithm']:
            kwargs[f'{provider}_using'] = index['algorithm'].lower()
        sa.Index(index['name'], *[table.c[column] for column in index['columns']], **kwargs)

    return table


def _build_column(
    field: Mapping[str, Any],
    provider: str,
    enum_types: Mapping[str, Any],
    *,
    is_primary_key: bool,
) -> sa.Column[Any]:
    if field['kind'] == 'enum':
        type_ = enum_types[field['type']]
    else:
        type_ = scalar_type(provider, field['type'])

    nullable = field['nullable']
    if field['is_list']:
        type_ = array_type(provider, type_)
        # Verified against `prisma db push`: a scalar list column is NULLable in
        # the database even though the DMMF reports the field as required.
        nullable = True

    default = field['default'] or {}
    # Explicit rather than SQLAlchemy's 'auto': `auto` makes any integer primary
    # key a SERIAL, but Prisma only does that for `@default(autoincrement())`.
    # An `Int @id` without it is a plain integer column.
    autoincrement = default.get('kind') == 'generator' and default.get('name') == 'autoincrement'

    return sa.Column(
        field['column'],
        type_,
        primary_key=is_primary_key,
        nullable=nullable,
        autoincrement=autoincrement,
        server_default=_server_default(field, enum_types),
    )


def _server_default(field: Mapping[str, Any], enum_types: Mapping[str, Any]) -> Optional[Any]:
    """The DEFAULT clause, or None when Prisma does not emit one.

    Most of the work here is *not* emitting things. `@default(cuid())` and
    `@updatedAt` are filled in by the client and leave no trace in the DDL, so
    inventing a server default for them diffs against every real database.
    """
    default = field['default']
    if default is None:
        return None

    if default['kind'] == 'generator':
        name = default['name']
        if name in _CLIENT_SIDE_GENERATORS:
            return None
        if name == 'autoincrement':
            # SERIAL, which SQLAlchemy emits from `autoincrement=True`
            return None
        if name == 'now':
            return sa.text('CURRENT_TIMESTAMP')
        if name == 'dbgenerated':
            args = default['args']
            if not args:
                # `dbgenerated()` with no argument is only legal for types
                # Prisma cannot otherwise express; there is nothing to emit.
                return None
            return sa.text(str(args[0]))
        raise NotImplementedError(f'Unhandled Prisma default generator: {name}()')

    return sa.text(_literal_sql(field, default['value'], enum_types))


def _literal_sql(field: Mapping[str, Any], value: Any, enum_types: Mapping[str, Any]) -> str:
    if field['kind'] == 'enum':
        enum = enum_types[field['type']]
        if field['is_list']:
            labels = ', '.join(_quote(str(item)) for item in value)
            return f'ARRAY[{labels}]::"{enum.name}"[]'
        # the DMMF sends the *Python* member name; the database stores the label
        return f'{_quote(str(value))}::"{enum.name}"'

    if field['is_list']:
        return 'ARRAY[' + ', '.join(_scalar_sql(field['type'], item) for item in value) + ']'

    return _scalar_sql(field['type'], value)


def _scalar_sql(prisma_type: str, value: Any) -> str:
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if prisma_type in {'Int', 'BigInt', 'Float', 'Decimal'}:
        # BigInt arrives as a string because JSON has no 64-bit integer
        return str(value)
    return _quote(str(value))


def _quote(value: str) -> str:
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def _add_foreign_keys(
    md: sa.MetaData,
    schema: Mapping[str, Any],
    spec: Mapping[str, Any],
) -> None:
    table = md.tables[_qualified(md, spec['table'])]

    for relation in spec['relations'].values():
        # Only the side that owns the foreign key declares it; the inverse side
        # describes the same constraint and would create a duplicate.
        if not relation['owner']:
            continue

        target = schema[relation['to']]
        table.append_constraint(
            sa.ForeignKeyConstraint(
                relation['fk_columns'],
                [f'{target["table"]}.{column}' for column in relation['referenced_columns']],
                name=_fk_name(spec['table'], relation['fk_columns']),
                ondelete=_on_delete(relation),
                onupdate='CASCADE',
            )
        )


def _on_delete(relation: Mapping[str, Any]) -> str:
    """Prisma's referential action, including the defaults it applies silently.

    Verified against `prisma db push`: an undeclared `onDelete` is `RESTRICT`
    for a mandatory relation and `SET NULL` for an optional one — *not*
    `CASCADE`, and not `NO ACTION`.
    """
    declared = relation['on_delete']
    if declared is None:
        return 'RESTRICT' if relation['fk_required'] else 'SET NULL'

    try:
        return _REFERENTIAL_ACTIONS[declared]
    except KeyError:
        raise NotImplementedError(f'Unhandled referential action: {declared}') from None


def _fk_name(table: str, columns: Sequence[str]) -> str:
    return '_'.join([table, *columns, 'fkey'])


def _build_join_table(
    md: sa.MetaData,
    schema: Mapping[str, Any],
    relation: Mapping[str, Any],
    owner: str,
) -> sa.Table:
    """The table Prisma creates for an implicit many-to-many relation.

    Two NOT NULL columns `A` and `B`, no primary key, a unique index across the
    pair and a plain index on `B`. `A` belongs to whichever model sorts first by
    name — which for a self-relation is both of them, but that only makes the
    *direction* ambiguous, not the table, so it is still buildable here.
    """
    name = relation['join_table']
    left_model = min(relation['to'], owner)
    right_model = max(relation['to'], owner)

    left = schema[left_model]
    right = schema[right_model]

    table = sa.Table(
        name,
        md,
        sa.Column(JOIN_LEFT, _referenced_type(md, left), nullable=False),
        sa.Column(JOIN_RIGHT, _referenced_type(md, right), nullable=False),
        sa.ForeignKeyConstraint(
            [JOIN_LEFT],
            [f'{left["table"]}.{left["primary_key"]["columns"][0]}'],
            name=f'{name}_{JOIN_LEFT}_fkey',
            ondelete='CASCADE',
            onupdate='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            [JOIN_RIGHT],
            [f'{right["table"]}.{right["primary_key"]["columns"][0]}'],
            name=f'{name}_{JOIN_RIGHT}_fkey',
            ondelete='CASCADE',
            onupdate='CASCADE',
        ),
    )

    sa.Index(f'{name}_AB_unique', table.c[JOIN_LEFT], table.c[JOIN_RIGHT], unique=True)
    sa.Index(f'{name}_{JOIN_RIGHT}_index', table.c[JOIN_RIGHT])
    return table


def _referenced_type(md: sa.MetaData, model_spec: Mapping[str, Any]) -> Any:
    table = md.tables[_qualified(md, model_spec['table'])]
    return table.c[model_spec['primary_key']['columns'][0]].type


def _qualified(md: sa.MetaData, table: str) -> str:
    return f'{md.schema}.{table}' if md.schema else table
