"""Typed access to the physical database schema emitted by the generator.

Everything here describes what is in `metadata.SCHEMA`, which only exists when
the client was generated with `schemaMetadata = true`.

Why this exists
---------------

The generated client has never known what a table is called. It builds a
GraphQL-ish document naming *models* and *fields*, hands it to the Rust query
engine, and the engine — which parsed `schema.prisma` itself — turns those into
SQL. `metadata.py` therefore carried exactly two things: the set of model names,
and `field -> related model name`.

Anything that emits SQL without the engine needs considerably more: the table a
model maps to, the column a field maps to, which side of a relation holds the
foreign key, and whether that foreign key is nullable. That last one is not a
detail — it is what decides whether `disconnect` and `set` are legal operations
at all. Against a `NOT NULL` foreign key there is no row state that expresses
"disconnected", and Prisma answers those requests with P2014. A backend that
does not know the nullability will happily orphan rows instead.

Shapes
------

Relations are classified into four shapes, because each compiles differently::

    to-one-owner     this model holds the FK        -> filter/join on own column
    to-one-inverse   the other model holds the FK   -> join, correlated subquery
    to-many          the other model holds the FK   -> join, EXISTS/aggregate
    many-to-many     an implicit join table         -> two joins through `_Rel`

`to-one-inverse` and `to-many` are the same physical arrangement and differ only
in arity, but they differ in every generated query, so they are named apart.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, cast
from importlib import import_module
from typing_extensions import Literal, TypedDict

__all__ = (
    'RelationShape',
    'DefaultSpec',
    'FieldSchema',
    'RelationSchema',
    'ConstraintSchema',
    'UniqueSchema',
    'IndexFieldSchema',
    'IndexSchema',
    'ModelSchema',
    'EnumSchema',
    'SchemaNotAvailableError',
    'is_available',
    'get_schema',
    'get_enum_schema',
    'get_provider',
    'model_schema',
    'column_name',
    'enum_label',
    'relation',
    'primary_key_columns',
)

RelationShape = Literal[
    'to-one-owner',
    'to-one-inverse',
    'to-many',
    'many-to-many',
]


class DefaultSpec(TypedDict, total=False):
    #: ``generator`` for ``cuid()``/``uuid()``/``now()``/``autoincrement()``/...,
    #: ``literal`` for a constant. Conflating the two silently turns a String
    #: field defaulting to ``"uuid"`` into a uuid generator.
    kind: Literal['generator', 'literal']
    #: generator only: ``cuid``, ``uuid``, ``now``, ``autoincrement``, ``dbgenerated``, ...
    #: The version is split off into ``version``: Prisma normalises
    #: ``@default(uuid())`` to ``uuid(4)`` on the wire, so the schema text and
    #: the DMMF disagree and matching on the raw name misses every such field.
    name: str
    #: generator only: ``'4'`` for ``uuid(4)``, ``None`` when unversioned
    version: Optional[str]
    args: List[Any]
    #: literal only. Two shapes to watch: an enum default is the *Python* member
    #: name and must go through `enum_label` before it reaches SQL, and a BigInt
    #: default arrives as a string because JSON has no 64-bit integer.
    value: Any


class FieldSchema(TypedDict):
    column: str
    #: ``scalar`` or ``enum``
    kind: str
    #: the Prisma type name (``String``, ``DateTime``, ...) or the enum name
    type: str
    is_list: bool
    nullable: bool
    is_id: bool
    is_unique: bool
    is_read_only: bool
    is_updated_at: bool
    default: Optional[DefaultSpec]
    #: ``@db.*`` as ``[type, args]``, e.g. ``['VarChar', ['255']]``. Prisma does
    #: not send this through the generator protocol at all, so it is recovered by
    #: lexing the raw schema text; ``None`` when the field is unannotated.
    native_type: Optional[List[Any]]
    #: The sequence backing a **non-primary-key** ``@default(autoincrement())``.
    #: A primary key gets ``SERIAL``, which creates its sequence implicitly; any
    #: other column needs this named explicitly or it ends up ``NOT NULL`` with
    #: no default and every INSERT fails.
    sequence: Optional[str]


class _RelationCommon(TypedDict):
    """Keys present on every relation, whatever its shape."""

    to: str
    shape: RelationShape
    relation_name: str
    is_list: bool
    nullable: bool
    #: whether *this* model holds the foreign key columns
    owner: bool
    back_field: Optional[str]
    #: the model whose table holds the foreign key, for either side of the
    #: relation; ``None`` for many-to-many
    fk_model: Optional[str]
    fk_fields: List[str]
    fk_columns: List[str]
    referenced_fields: List[str]
    referenced_columns: List[str]
    #: True when every foreign key column is NOT NULL, i.e. the relation is
    #: mandatory and `disconnect`/`set`-that-drops must raise P2014
    fk_required: bool
    #: ``None`` means "Prisma's default for this arity" — Restrict when required,
    #: SetNull when optional — not "no action"
    on_delete: Optional[str]
    #: The foreign key constraint name, on the owning side; ``None`` otherwise.
    #: ``@relation(map:)`` when set — Prisma does not send that in the DMMF, so it
    #: is lexed from the schema text — else ``<table>_<columns>_fkey`` truncated
    #: to 63 characters the way Prisma truncates it.
    fk_name: Optional[str]
    #: set for a self-referential many-to-many, where which side is column `A`
    #: is not recoverable from the DMMF. Consumers must refuse rather than guess.
    #: Present on **every** relation, ``False`` where trivially so, so that
    #: checking it never raises.
    join_ambiguous: bool


class RelationSchema(_RelationCommon, total=False):
    """A relation. The join keys are present only when ``shape`` is
    ``many-to-many``; everything inherited above is always there, so a type
    checker does not force a guard around the common keys."""

    join_table: str
    join_self_column: Optional[str]
    join_other_column: Optional[str]


class ConstraintSchema(TypedDict):
    #: the *Prisma* identifier for a compound `@@id`, ``None`` for a single `@id`
    name: Optional[str]
    #: `@@id(map: ...)`. ``None`` means the provider's default, which is
    #: ``<table>_pkey`` on PostgreSQL but ``PRIMARY`` on MySQL — deliberately not
    #: resolved at generation time.
    db_name: Optional[str]
    fields: List[str]
    columns: List[str]


class UniqueSchema(TypedDict):
    #: the *Prisma* identifier, i.e. the key used in
    #: ``where={'email_tenantId': ...}``
    name: str
    #: the constraint name in the database. A different namespace to ``name``;
    #: resolved to Prisma's default when the schema does not map it.
    db_name: str
    fields: List[str]
    columns: List[str]


class IndexFieldSchema(TypedDict):
    name: str
    sort_order: Optional[str]
    length: Optional[int]
    operator_class: Optional[str]


class IndexSchema(TypedDict):
    #: resolved to Prisma's default (``table_col_idx``) when the schema does not
    #: name it, so a schema differ compares real names
    name: str
    #: False when `name` above was derived rather than declared
    is_named: bool
    #: ``normal`` or ``fulltext``; ``id``/``unique`` entries are reported as
    #: constraints instead so a migration tool does not create both
    type: str
    algorithm: Optional[str]
    clustered: Optional[bool]
    columns: List[str]
    #: parallel to ``columns``, one entry per index member. Carries the per-column
    #: sort direction from ``@@index([a, b(sort: Desc)])``; dropping it silently
    #: changes which queries the index can serve.
    fields: List[IndexFieldSchema]


class ModelSchema(TypedDict):
    table: str
    primary_key: ConstraintSchema
    fields: Dict[str, FieldSchema]
    relations: Dict[str, RelationSchema]
    uniques: List[UniqueSchema]
    indexes: List[IndexSchema]


class EnumSchema(TypedDict):
    db_name: str
    #: python name -> stored label. These differ under `@map`, and writing the
    #: python name to the database fails.
    values: Dict[str, str]


class SchemaNotAvailableError(RuntimeError):
    def __init__(self) -> None:
        super().__init__(
            'The generated client does not include physical schema metadata.\n'
            '  Add `schemaMetadata = true` to your generator block and re-run `prisma generate`, e.g.\n'
            '\n'
            '    generator client {\n'
            '      provider       = "prisma-client-py"\n'
            '      schemaMetadata = true\n'
            '    }\n'
        )


def _metadata() -> Any:
    return import_module('prisma.metadata')


def is_available() -> bool:
    """Whether the client was generated with `schemaMetadata = true`."""
    return hasattr(_metadata(), 'SCHEMA')


def get_schema() -> Mapping[str, ModelSchema]:
    """model name -> its physical schema."""
    schema = getattr(_metadata(), 'SCHEMA', None)
    if schema is None:
        raise SchemaNotAvailableError()
    return cast('Mapping[str, ModelSchema]', schema)


def get_enum_schema() -> Mapping[str, EnumSchema]:
    """enum name -> its database name and value mappings."""
    schema = getattr(_metadata(), 'ENUM_SCHEMA', None)
    if schema is None:
        raise SchemaNotAvailableError()
    return cast('Mapping[str, EnumSchema]', schema)


def get_provider() -> str:
    """The active datasource provider, e.g. `postgresql`."""
    provider = getattr(_metadata(), 'DATABASE_PROVIDER', None)
    if provider is None:
        raise SchemaNotAvailableError()
    return cast(str, provider)


def model_schema(model: str) -> ModelSchema:
    try:
        return get_schema()[model]
    except KeyError:
        raise LookupError(f'Unknown model: {model}') from None


def column_name(model: str, field: str) -> str:
    """The column a scalar field maps to, honouring `@map`."""
    schema = model_schema(model)
    try:
        return schema['fields'][field]['column']
    except KeyError:
        if field in schema['relations']:
            raise LookupError(
                f'{model}.{field} is a relation, not a column; ' f'use the relation metadata to resolve its foreign key'
            ) from None
        raise LookupError(f'Unknown field: {model}.{field}') from None


def enum_label(enum: str, value: str) -> str:
    """The stored label for an enum member, honouring `@map`."""
    try:
        return get_enum_schema()[enum]['values'][value]
    except KeyError:
        raise LookupError(f'Unknown enum member: {enum}.{value}') from None


def relation(model: str, field: str) -> RelationSchema:
    try:
        return model_schema(model)['relations'][field]
    except KeyError:
        raise LookupError(f'Unknown relation: {model}.{field}') from None


def primary_key_columns(model: str) -> Sequence[str]:
    """Columns forming the primary key.

    Empty for a model identified only by a unique constraint, which Prisma
    permits; callers that need a row identity must fall back to `uniques`.
    """
    return model_schema(model)['primary_key']['columns']
