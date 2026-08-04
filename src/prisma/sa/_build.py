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

from typing import Any, Dict, List, Mapping, Optional

import sqlalchemy as sa
from sqlalchemy import event

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
        args.append(_build_column(field, provider, enum_types, md, is_primary_key=name in primary_key))

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
        sa.Index(index['name'], *_index_expressions(table, index), **kwargs)

    _own_sequences(table, spec)
    return table


def _index_expressions(table: sa.Table, index: Mapping[str, Any]) -> List[Any]:
    """Index columns, carrying `sort: Desc` through as `column.desc()`.

    Dropping the direction is nearly free on a single-column index — PostgreSQL
    scans backwards — but not on a composite one: `(a ASC, b ASC)` read backwards
    is `(a DESC, b DESC)`, which serves neither `ORDER BY a, b DESC` nor
    `ORDER BY a DESC, b`. The index the schema asked for cannot be substituted by
    the one that would be built, so the query falls back to a sort node. Silent
    to the application, visible later as a slow query.
    """
    # `columns` and `fields` are built together and stay parallel: one entry per
    # index member, in declaration order.
    orders = [(field.get('sort_order') or '').lower() for field in index['fields']]
    expressions: List[Any] = []

    for position, column_name in enumerate(index['columns']):
        column = table.c[column_name]
        descending = position < len(orders) and orders[position] == 'desc'
        expressions.append(column.desc() if descending else column)

    return expressions


def _build_column(
    field: Mapping[str, Any],
    provider: str,
    enum_types: Mapping[str, Any],
    metadata: sa.MetaData,
    *,
    is_primary_key: bool,
) -> sa.Column[Any]:
    if field['kind'] == 'enum':
        type_ = enum_types[field['type']]
    else:
        # `@db.*` wins over the default scalar mapping. Ignoring it turns every
        # `@db.Uuid` key into `text`, which is a table rewrite to correct later.
        type_ = scalar_type(provider, field['type'], field.get('native_type'))

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

    args: List[Any] = [field['column'], type_]
    server_default = _server_default(field, enum_types)

    # A non-primary-key `@default(autoincrement())` needs an explicit sequence.
    # SQLAlchemy only emits SERIAL for an integer *primary* key; on any other
    # column it treats a `Sequence` as a client-side pre-execute default and
    # emits no DDL default at all. The column then lands NOT NULL with nothing
    # to fill it and every INSERT into that table fails — silently, because
    # Alembic does not compare server defaults by default, so autogenerate still
    # reports an empty diff.
    sequence = field.get('sequence')
    if sequence:
        args.append(sa.Sequence(sequence, data_type=_sequence_data_type(field), metadata=metadata))
        # quoted because Prisma's names are case-sensitive
        server_default = sa.text(f'nextval(\'"{sequence}"\'::regclass)')

    return sa.Column(
        *args,
        primary_key=is_primary_key,
        nullable=nullable,
        autoincrement=autoincrement,
        server_default=server_default,
    )


def _sequence_data_type(field: Mapping[str, Any]) -> Any:
    """`CREATE SEQUENCE … AS integer` vs the bigint default.

    Prisma sizes the sequence to the column: `Int` gives `AS integer`, `BigInt`
    gives the `bigint` SQLAlchemy would default to anyway. Omitting it makes
    every `Int` sequence a bigint one, which is a diff on every such column.
    """
    return sa.BigInteger() if field['type'] == 'BigInt' else sa.Integer()


def _own_sequences(table: sa.Table, spec: Mapping[str, Any]) -> None:
    """`ALTER SEQUENCE … OWNED BY …`, which SQLAlchemy has no API for.

    Ownership is what makes the sequence disappear with the column and what
    `pg_get_serial_sequence` looks at; `SERIAL` sets it implicitly and an
    explicit `Sequence` does not. Without it the sequence outlives a dropped
    column and shows up as a diff.
    """
    for field in spec['fields'].values():
        sequence = field.get('sequence')
        if not sequence:
            continue
        event.listen(
            table,
            'after_create',
            # SQLAlchemy leaves `DDL.__init__` unannotated, so mypy sees an
            # untyped call here.
            sa.DDL(  # type: ignore[no-untyped-call]
                f'ALTER SEQUENCE "{sequence}" OWNED BY "{table.name}"."{field["column"]}"'
            ),
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
        # Prisma normalises `uuid()` to `uuid(4)` on the wire; the generator
        # already strips the version, but be defensive — an unstripped name here
        # falls through to the `raise` and blocks the whole schema.
        name = default['name'].split('(', 1)[0]
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
        # The cast is not optional. PostgreSQL rejects a bare `ARRAY[]` with
        # `cannot determine type of empty array`, and `create_all()` aborts on
        # the first such table. Prisma emits `ARRAY[]::uuid[]`.
        elements = ', '.join(_scalar_sql(field['type'], item) for item in value)
        return f'ARRAY[{elements}]::{_array_element_sql_type(field)}[]'

    return _scalar_sql(field['type'], value)


#: Prisma scalar -> the SQL type name to cast an array literal to. Only reached
#: for scalar list defaults, which Prisma allows on PostgreSQL only.
_ARRAY_ELEMENT_TYPES = {
    'String': 'text',
    'Boolean': 'boolean',
    'Int': 'integer',
    'BigInt': 'bigint',
    'Float': 'double precision',
    'Decimal': 'numeric',
    'DateTime': 'timestamp',
    'Json': 'jsonb',
    'Bytes': 'bytea',
}


def _array_element_sql_type(field: Mapping[str, Any]) -> str:
    native = field.get('native_type')
    if native:
        # `readBy String[] @default([]) @db.Uuid` -> `ARRAY[]::uuid[]`
        return str(native[0]).lower()

    try:
        return _ARRAY_ELEMENT_TYPES[field['type']]
    except KeyError:
        raise NotImplementedError(
            f'No array element type for Prisma type {field["type"]!r}; '
            'an uncast array literal is a runtime error, not a diff'
        ) from None


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
                name=relation['fk_name'],
                ondelete=_on_delete(relation),
                onupdate=_on_update(relation),
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

    return _referential_action(declared)


def _on_update(relation: Mapping[str, Any]) -> str:
    """The same, for `ON UPDATE`, where the default does *not* vary by arity.

    Verified against `prisma db push`: an undeclared `onUpdate` is `CASCADE` for
    a mandatory relation and for an optional one alike. That is what made
    hardcoding `CASCADE` here look right — and it is right, up until a schema
    declares something else, which Prisma honours and which nothing downstream
    could see, because `relationOnUpdate` is not in the DMMF at all. The declared
    action is lexed out of the raw schema text instead.

    `.get` rather than `[...]`: a client generated before `on_update` existed
    carries no such key, and its absence means exactly what a declared `None`
    means.
    """
    declared = relation.get('on_update')
    if declared is None:
        return 'CASCADE'

    return _referential_action(declared)


def _referential_action(declared: str) -> str:
    try:
        return _REFERENTIAL_ACTIONS[declared]
    except KeyError:
        raise NotImplementedError(f'Unhandled referential action: {declared}') from None


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

    # Prisma owns both of the join table's foreign keys and rejects a schema that
    # tries to say otherwise — "Referential actions on implicit many-to-many
    # relations are not supported", verified against 5.19 — so `on_update` is
    # necessarily absent here and this resolves to Prisma's default, CASCADE. It
    # is still read through the same helper so the two can never drift apart.
    # `ondelete` is *not*: `_on_delete` keys off `fk_required`, which no side of
    # an implicit m2m carries, and would answer SET NULL for columns Prisma
    # creates NOT NULL.
    on_update = _on_update(relation)

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
            onupdate=on_update,
        ),
        sa.ForeignKeyConstraint(
            [JOIN_RIGHT],
            [f'{right["table"]}.{right["primary_key"]["columns"][0]}'],
            name=f'{name}_{JOIN_RIGHT}_fkey',
            ondelete='CASCADE',
            onupdate=on_update,
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
