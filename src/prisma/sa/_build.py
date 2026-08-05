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

import re
from typing import Any, Dict, List, Tuple, Mapping, Optional

import sqlalchemy as sa
from sqlalchemy import event

from ._types import enum_type, array_type, scalar_type, check_provider

__all__ = (
    'build_metadata',
    'JOIN_LEFT',
    'JOIN_RIGHT',
    'IndexPrefixLengthError',
    'UnknownOperatorClassError',
    'SortedOperatorClassError',
)

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

#: PostgreSQL's `NAMEDATALEN - 1`, and what both Prisma and the server truncate
#: derived constraint names to.
_MAX_IDENTIFIER_LENGTH = 63

#: Prisma's built-in operator class names -> the PostgreSQL operator class.
#:
#: Measured, not transcribed: every name Prisma 5.19 accepts on PostgreSQL was
#: pushed with `prisma db push` and the resulting `pg_opclass.opcname` read back
#: out of the catalog. That matters because the transformation is not the
#: regular one it looks like — `Int4MinMaxOps` is `int4_minmax_ops`, not
#: `int4_min_max_ops`, and `VarBitMinMaxOps` is `varbit_…`, not `var_bit_…`.
#: `TextMinMaxMultiOps` is absent because Prisma rejects it; the schema does not
#: compile.
_OPERATOR_CLASSES = {
    # gist / gin / spgist
    'InetOps': 'inet_ops',
    'ArrayOps': 'array_ops',
    'JsonbOps': 'jsonb_ops',
    'JsonbPathOps': 'jsonb_path_ops',
    'TextOps': 'text_ops',
    # brin
    'BitMinMaxOps': 'bit_minmax_ops',
    'VarBitMinMaxOps': 'varbit_minmax_ops',
    'BpcharBloomOps': 'bpchar_bloom_ops',
    'BpcharMinMaxOps': 'bpchar_minmax_ops',
    'ByteaBloomOps': 'bytea_bloom_ops',
    'ByteaMinMaxOps': 'bytea_minmax_ops',
    'DateBloomOps': 'date_bloom_ops',
    'DateMinMaxOps': 'date_minmax_ops',
    'DateMinMaxMultiOps': 'date_minmax_multi_ops',
    'Float4BloomOps': 'float4_bloom_ops',
    'Float4MinMaxOps': 'float4_minmax_ops',
    'Float4MinMaxMultiOps': 'float4_minmax_multi_ops',
    'Float8BloomOps': 'float8_bloom_ops',
    'Float8MinMaxOps': 'float8_minmax_ops',
    'Float8MinMaxMultiOps': 'float8_minmax_multi_ops',
    'InetInclusionOps': 'inet_inclusion_ops',
    'InetBloomOps': 'inet_bloom_ops',
    'InetMinMaxOps': 'inet_minmax_ops',
    'InetMinMaxMultiOps': 'inet_minmax_multi_ops',
    'Int2BloomOps': 'int2_bloom_ops',
    'Int2MinMaxOps': 'int2_minmax_ops',
    'Int2MinMaxMultiOps': 'int2_minmax_multi_ops',
    'Int4BloomOps': 'int4_bloom_ops',
    'Int4MinMaxOps': 'int4_minmax_ops',
    'Int4MinMaxMultiOps': 'int4_minmax_multi_ops',
    'Int8BloomOps': 'int8_bloom_ops',
    'Int8MinMaxOps': 'int8_minmax_ops',
    'Int8MinMaxMultiOps': 'int8_minmax_multi_ops',
    'NumericBloomOps': 'numeric_bloom_ops',
    'NumericMinMaxOps': 'numeric_minmax_ops',
    'NumericMinMaxMultiOps': 'numeric_minmax_multi_ops',
    'OidBloomOps': 'oid_bloom_ops',
    'OidMinMaxOps': 'oid_minmax_ops',
    'OidMinMaxMultiOps': 'oid_minmax_multi_ops',
    'TextBloomOps': 'text_bloom_ops',
    'TextMinMaxOps': 'text_minmax_ops',
    'TimestampBloomOps': 'timestamp_bloom_ops',
    'TimestampMinMaxOps': 'timestamp_minmax_ops',
    'TimestampMinMaxMultiOps': 'timestamp_minmax_multi_ops',
    'TimestampTzBloomOps': 'timestamptz_bloom_ops',
    'TimestampTzMinMaxOps': 'timestamptz_minmax_ops',
    'TimestampTzMinMaxMultiOps': 'timestamptz_minmax_multi_ops',
    'TimeBloomOps': 'time_bloom_ops',
    'TimeMinMaxOps': 'time_minmax_ops',
    'TimeMinMaxMultiOps': 'time_minmax_multi_ops',
    'TimeTzBloomOps': 'timetz_bloom_ops',
    'TimeTzMinMaxOps': 'timetz_minmax_ops',
    'TimeTzMinMaxMultiOps': 'timetz_minmax_multi_ops',
    'UuidBloomOps': 'uuid_bloom_ops',
    'UuidMinMaxOps': 'uuid_minmax_ops',
    'UuidMinMaxMultiOps': 'uuid_minmax_multi_ops',
}

#: What a Prisma *built-in* operator class name looks like. `ops: raw("...")`
#: arrives on the wire in the same field and is indistinguishable by position —
#: the raw text is passed straight through — so the two are told apart by shape.
#: Every built-in Prisma defines matches this and no PostgreSQL operator class
#: name does; they are lower snake case (`text_pattern_ops`, `gin_trgm_ops`).
_BUILT_IN_OPERATOR_CLASS = re.compile(r'^[A-Z][A-Za-z0-9]*Ops$')


class IndexPrefixLengthError(NotImplementedError):
    """`@@index([col(length: n)])`, which PostgreSQL has no equivalent for.

    Prisma rejects the annotation outright on this provider — "The length
    argument is not supported in an index definition with the current
    connector", verified against 5.19 — so a payload carrying one did not come
    from a PostgreSQL schema, and the index it asks for cannot be built.
    """

    def __init__(self, index: str, column: str, length: int) -> None:
        self.index = index
        self.column = column
        self.length = length
        super().__init__(
            f'Index {index!r} asks for a prefix length of {length} on column {column!r}.\n'
            '  That is a MySQL feature; PostgreSQL indexes the whole value and Prisma rejects\n'
            '  `length:` on this provider, so there is no index to build that matches.\n'
            '  Building one without the prefix would silently index something else.'
        )


class UnknownOperatorClassError(NotImplementedError):
    """A built-in `ops:` name with no measured PostgreSQL operator class.

    Passing the Prisma name through would emit `USING btree (col SomeNewOps)`,
    which is a `CREATE INDEX` failure at best and the wrong operator class at
    worst.
    """

    def __init__(self, index: str, column: str, operator_class: str) -> None:
        self.index = index
        self.column = column
        self.operator_class = operator_class
        super().__init__(
            f'Index {index!r} uses the Prisma operator class {operator_class!r} on column {column!r},\n'
            '  which has no verified PostgreSQL equivalent here.\n'
            '  Add it to prisma/sa/_build.py, read back out of `pg_opclass` after a real\n'
            '  `prisma db push` — the name is not derivable from the Prisma one.'
        )


class SortedOperatorClassError(NotImplementedError):
    """`ops:` and `sort: Desc` on the same index member.

    Prisma emits `(col opclass DESC)`. SQLAlchemy's `postgresql_ops` appends the
    operator class *after* the compiled expression, so the pair would render as
    `(col DESC opclass)` — and in practice not at all, because the lookup is
    keyed on a plain column and a `.desc()` expression carries no such key. The
    index would be built silently without its operator class.
    """

    def __init__(self, index: str, column: str, operator_class: str) -> None:
        self.index = index
        self.column = column
        self.operator_class = operator_class
        super().__init__(
            f'Index {index!r} declares both `sort: Desc` and an operator class '
            f'({operator_class!r}) on column {column!r}.\n'
            '  Prisma builds `(col opclass DESC)`; SQLAlchemy cannot express that ordering —\n'
            '  `postgresql_ops` is appended after the expression and is dropped entirely on a\n'
            '  descending one, which builds a different index without saying so.'
        )


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

    primary_key_constraint = _primary_key_constraint(spec)
    if primary_key_constraint is not None:
        args.append(primary_key_constraint)

    table = sa.Table(spec['table'], md, *args)

    # A unique *index*, not a `UniqueConstraint`. Verified against `prisma db
    # push`: Prisma emits `CREATE UNIQUE INDEX` even on PostgreSQL, where a
    # unique constraint would be the more idiomatic choice. Alembic tells the
    # two apart, so a constraint here means a drop-and-recreate on every
    # migration.
    #
    # A unique takes the same per-column modifiers an index does — `@@unique([a,
    # b(sort: Desc)])` and a field-level `@unique(sort: Desc)` both build a
    # descending member — so both go through the same builder. `.get` for the
    # modifiers: a client generated before they were reported carries no such
    # key, and no modifiers is what every such schema meant.
    for unique in spec['uniques']:
        _build_index(
            table,
            unique['db_name'],
            unique['columns'],
            unique.get('field_modifiers') or [],
            provider,
            unique=True,
        )

    for index in spec['indexes']:
        kwargs: Dict[str, Any] = {}
        if index['algorithm']:
            kwargs[f'{provider}_using'] = index['algorithm'].lower()
        _build_index(table, index['name'], index['columns'], index['fields'], provider, **kwargs)

    _own_sequences(table, spec)
    return table


def _primary_key_constraint(spec: Mapping[str, Any]) -> Optional[sa.PrimaryKeyConstraint]:
    """The primary key, stated explicitly so its column *order* is ours.

    `@@id([right, left])` on a model that declares `left` first is
    `PRIMARY KEY ("right", "left")` — verified against `prisma db push`. The
    per-column `primary_key=True` flags cannot express that: SQLAlchemy takes
    the order from the column definitions, which is DMMF field order, and the
    key it builds still works while indexing the pair the other way round.

    Naming it is a separate question from stating it. An unnamed constraint
    compiles to a bare `PRIMARY KEY (...)`, which is what PostgreSQL already
    gets, so the usual case adds no name: `@@id(map:)` when the schema mapped
    one, and otherwise a name only when the declared order differs from the
    column order. That last case needs one because a name is the only part of
    the constraint that survives into a declarative model — `_declarative.py`
    spells the constraint out exactly when it is named, and otherwise leaves the
    key to the `primary_key=True` flags, i.e. back to field order.
    """
    primary_key = spec['primary_key']
    columns = list(primary_key['columns'])
    if not columns:
        # Prisma allows a model identified only by a unique constraint.
        return None

    in_field_order = [field['column'] for field in spec['fields'].values() if field['column'] in set(columns)]
    name = primary_key['db_name']
    if name is None and columns != in_field_order:
        name = _default_primary_key_name(spec['table'])

    return sa.PrimaryKeyConstraint(*columns, name=name)


def _default_primary_key_name(table: str) -> str:
    """`<table>_pkey`, truncated the way the server truncates it.

    PostgreSQL-specific, which is why the generator refuses to resolve it —
    MySQL calls the constraint `PRIMARY`. It is resolvable here because
    `check_provider` has already run and PostgreSQL is the only provider this
    builds for. Measured against an 82-character table name: `prisma db push`
    leaves the naming to the server and the server keeps the suffix and cuts the
    base, which is also Prisma's own rule for the names it derives.
    """
    return _truncate_identifier(table, '_pkey')


def _truncate_identifier(base: str, suffix: str) -> str:
    """Prisma's rule, and the server's: keep the suffix, cut the base.

    Deliberately not imported from `prisma.generator.models`, which is the
    generator's Pydantic model tree and has no business being loaded at runtime
    by a client that only wants a `MetaData`. The two copies are pinned against
    each other in `test_build.py`, since the whole point of the rule is that
    everything derived from a long name agrees on the same 63 characters.
    """
    name = f'{base}{suffix}'
    if len(name) <= _MAX_IDENTIFIER_LENGTH:
        return name
    return base[: _MAX_IDENTIFIER_LENGTH - len(suffix)] + suffix


def _build_index(
    table: sa.Table,
    name: str,
    columns: List[str],
    fields: List[Mapping[str, Any]],
    provider: str,
    **kwargs: Any,
) -> sa.Index:
    expressions, operator_classes = _index_expressions(table, name, columns, fields)
    if operator_classes:
        kwargs[f'{provider}_ops'] = operator_classes
    return sa.Index(name, *expressions, **kwargs)


def _index_expressions(
    table: sa.Table,
    name: str,
    columns: List[str],
    fields: List[Mapping[str, Any]],
) -> Tuple[List[Any], Dict[str, str]]:
    """Index members, and the operator classes to attach to them.

    `sort: Desc` becomes `column.desc()`. Dropping the direction is nearly free
    on a single-column index — PostgreSQL scans backwards — but not on a
    composite one: `(a ASC, b ASC)` read backwards is `(a DESC, b DESC)`, which
    serves neither `ORDER BY a, b DESC` nor `ORDER BY a DESC, b`. The index the
    schema asked for cannot be substituted by the one that would be built, so the
    query falls back to a sort node. Silent to the application, visible later as
    a slow query.

    `ops:` becomes an entry in `postgresql_ops`, which is a different kind of
    wrong to get wrong: `@@index([slug(ops: raw("text_pattern_ops"))])` is what
    makes an index usable by `LIKE 'prefix%'` under a non-C collation, and
    without it the index exists, matches on name, and never serves the query it
    was written for. `length:` has no PostgreSQL equivalent at all and raises.
    """
    # `columns` and `fields` are built together and stay parallel: one entry per
    # index member, in declaration order.
    expressions: List[Any] = []
    operator_classes: Dict[str, str] = {}

    for position, column_name in enumerate(columns):
        modifiers: Mapping[str, Any] = fields[position] if position < len(fields) else {}
        column = table.c[column_name]

        length = modifiers.get('length')
        if length is not None:
            raise IndexPrefixLengthError(name, column_name, length)

        descending = (modifiers.get('sort_order') or '').lower() == 'desc'
        operator_class = _operator_class(name, column_name, modifiers.get('operator_class'))
        if operator_class is not None:
            if descending:
                raise SortedOperatorClassError(name, column_name, operator_class)
            # keyed on `Column.key`, which is what the PostgreSQL dialect looks
            # the operator class up by when it compiles the index
            operator_classes[column.key] = operator_class

        expressions.append(column.desc() if descending else column)

    return expressions, operator_classes


def _operator_class(index: str, column: str, declared: Optional[str]) -> Optional[str]:
    """The PostgreSQL operator class for one index member, or None.

    Prisma sends `ops: raw("text_pattern_ops")` and `ops: JsonbPathOps` in the
    same field: the raw form arrives as its literal text, the built-in one as
    Prisma's own name, which is not a PostgreSQL identifier and has to be
    translated. Anything shaped like a built-in but absent from the measured
    table is refused rather than passed through, because passing it through
    emits SQL naming an operator class that does not exist.
    """
    if declared is None:
        return None
    if not _BUILT_IN_OPERATOR_CLASS.match(declared):
        # `raw("...")`, which is already the database's own spelling
        return declared
    try:
        return _OPERATOR_CLASSES[declared]
    except KeyError:
        raise UnknownOperatorClassError(index, column, declared) from None


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

        if not _emits_foreign_keys(relation):
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


def _emits_foreign_keys(relation: Mapping[str, Any]) -> bool:
    """Whether this relation is backed by a foreign key in the database.

    `relationMode = "prisma"` means it is not: Prisma creates **no** foreign key
    constraints — measured against `prisma db push` 5.19 on PostgreSQL, on model
    tables and on implicit many-to-many join tables alike — and enforces
    relations in the query engine instead. That is how PlanetScale and Vitess
    users run.

    Emitting the constraints anyway describes a database that does not exist:
    `alembic revision --autogenerate` proposes to *add* one per relation, and
    the DDL-equivalence gate fails on every table.

    Prisma creates no index in their place either. Measured on the same push: an
    unindexed relation scalar stays unindexed, and Prisma only warns — "relation
    fields will not benefit from the index usually created by the relational
    database under the hood ... We recommend adding an index manually". So the
    right output here is nothing at all, not an index standing in for the
    constraint; a schema that wants one writes `@@index`, and that arrives
    through the normal index path.

    `.get` rather than `[...]`: a client generated before `relation_mode` existed
    carries no such key, and its absence means exactly what a declared `None`
    means — the datasource is silent, which is Prisma's default of
    `foreignKeys`.
    """
    mode = relation.get('relation_mode')
    if mode is None or mode == 'foreignKeys':
        return True
    if mode == 'prisma':
        return False

    # Prisma accepts only those two. Guessing at a third would mean guessing at
    # whether a whole schema's constraints exist.
    raise NotImplementedError(
        f'Unhandled relationMode: {mode!r}; expected "foreignKeys" or "prisma". '
        'Whether the database has foreign keys at all cannot be guessed at.'
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

    args: List[Any] = [
        sa.Column(JOIN_LEFT, _referenced_type(md, left), nullable=False),
        sa.Column(JOIN_RIGHT, _referenced_type(md, right), nullable=False),
    ]

    # Under `relationMode = "prisma"` the join table has no foreign keys either —
    # measured, and easy to miss, because the join table is not a model and
    # nothing in the schema text mentions its constraints.
    if _emits_foreign_keys(relation):
        # Prisma owns both of the join table's foreign keys and rejects a schema
        # that tries to say otherwise — "Referential actions on implicit
        # many-to-many relations are not supported", verified against 5.19 — so
        # `on_update` is necessarily absent here and this resolves to Prisma's
        # default, CASCADE. It is still read through the same helper so the two
        # can never drift apart. `ondelete` is *not*: `_on_delete` keys off
        # `fk_required`, which no side of an implicit m2m carries, and would
        # answer SET NULL for columns Prisma creates NOT NULL.
        on_update = _on_update(relation)
        args.extend(
            [
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
            ]
        )

    table = sa.Table(name, md, *args)

    sa.Index(f'{name}_AB_unique', table.c[JOIN_LEFT], table.c[JOIN_RIGHT], unique=True)
    sa.Index(f'{name}_{JOIN_RIGHT}_index', table.c[JOIN_RIGHT])
    return table


def _referenced_type(md: sa.MetaData, model_spec: Mapping[str, Any]) -> Any:
    table = md.tables[_qualified(md, model_spec['table'])]
    return table.c[model_spec['primary_key']['columns'][0]].type


def _qualified(md: sa.MetaData, table: str) -> str:
    return f'{md.schema}.{table}' if md.schema else table
