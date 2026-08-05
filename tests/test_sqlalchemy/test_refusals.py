"""Cases where the builder must raise rather than emit something plausible.

Every one of these has a tempting wrong answer. A wrong type table or an
invented referential action does not fail here — it fails as a schema diff on
someone else's database during a deploy, which is both later and harder to
attribute. So each is pinned to a named exception with the offending value in
the message.

Built from hand-written metadata dicts rather than a recorded schema, because
most of these shapes cannot be expressed in a `.prisma` file at all: Prisma
would reject `Json` on SQLite before the generator ever ran.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest
import sqlalchemy as sa

from prisma.sa import build_metadata
from prisma.sa._types import SUPPORTED_PROVIDERS, UnsupportedProviderError


def server_default(metadata: sa.MetaData, table: str, column: str) -> str:
    """The DDL default expression for a column.

    `Column.server_default` is declared as the `FetchedValue` base, which has
    no `.arg`; only the `DefaultClause` subclass carries the expression.
    """
    default = metadata.tables[table].c[column].server_default
    assert default is not None
    assert isinstance(default, sa.DefaultClause)
    return str(default.arg)


def field(**overrides: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        'column': 'id',
        'kind': 'scalar',
        'type': 'String',
        'is_list': False,
        'nullable': False,
        'is_id': True,
        'is_unique': False,
        'is_read_only': False,
        'is_updated_at': False,
        'default': None,
        'native_type': None,
    }
    base.update(overrides)
    return base


def model(fields: Dict[str, Any], **overrides: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        'table': 'Thing',
        'primary_key': {'name': None, 'db_name': None, 'fields': ['id'], 'columns': ['id']},
        'fields': fields,
        'relations': {},
        'uniques': [],
        'indexes': [],
    }
    base.update(overrides)
    return base


def schema(**fields: Any) -> Dict[str, Any]:
    return {'Thing': model({'id': field(), **fields})}


# -- providers ----------------------------------------------------------------


@pytest.mark.parametrize('provider', ['mysql', 'sqlite', 'sqlserver', 'cockroachdb', 'mongodb'])
def test_unverified_providers_refuse(provider: str) -> None:
    """Only providers checked against a real `prisma db push` are claimed."""
    with pytest.raises(UnsupportedProviderError) as exc:
        build_metadata(schema(), {}, provider)

    assert provider in str(exc.value)
    # the message must say what *is* supported, or there is nothing to act on
    for supported in SUPPORTED_PROVIDERS:
        assert supported in str(exc.value)
    assert exc.value.provider == provider


def test_unsupported_provider_is_a_not_implemented_error() -> None:
    """Callers should be able to catch it without importing our exception."""
    assert issubclass(UnsupportedProviderError, NotImplementedError)


def test_supported_provider_list_is_not_aspirational() -> None:
    """A provider in this set must have a scalar type table behind it."""
    for provider in SUPPORTED_PROVIDERS:
        build_metadata(schema(), {}, provider)


# -- types --------------------------------------------------------------------


def test_unknown_scalar_type_refuses() -> None:
    """A Prisma release adding a scalar type must not silently map to nothing."""
    with pytest.raises(NotImplementedError, match="Prisma type 'Quaternion'"):
        build_metadata(schema(weird=field(column='weird', type='Quaternion', is_id=False)), {}, 'postgresql')


# -- defaults -----------------------------------------------------------------


def test_unknown_default_generator_refuses() -> None:
    """Silently dropping an unrecognised generator loses the column's default."""
    broken = field(column='id', default={'kind': 'generator', 'name': 'moonphase', 'args': []})
    with pytest.raises(NotImplementedError, match='moonphase'):
        build_metadata(schema(id=broken), {}, 'postgresql')


@pytest.mark.parametrize('name', ['cuid', 'uuid', 'nanoid', 'ulid', 'auto'])
def test_client_side_generators_are_accepted_and_emit_nothing(name: str) -> None:
    """These are filled in by the client; the column has no DDL default."""
    built = build_metadata(schema(id=field(default={'kind': 'generator', 'name': name, 'args': []})), {}, 'postgresql')
    assert built.tables['Thing'].c['id'].server_default is None


def test_dbgenerated_passes_the_expression_through() -> None:
    built = build_metadata(
        schema(id=field(default={'kind': 'generator', 'name': 'dbgenerated', 'args': ['gen_random_uuid()']})),
        {},
        'postgresql',
    )
    assert server_default(built, 'Thing', 'id') == 'gen_random_uuid()'


def test_dbgenerated_without_an_argument_emits_nothing() -> None:
    """`dbgenerated()` with no argument names a type Prisma cannot express.

    There is no expression to emit, and inventing one would be worse than the
    column simply having no default.
    """
    built = build_metadata(
        schema(id=field(default={'kind': 'generator', 'name': 'dbgenerated', 'args': []})), {}, 'postgresql'
    )
    assert built.tables['Thing'].c['id'].server_default is None


def test_string_literal_defaults_are_quoted() -> None:
    built = build_metadata(
        schema(name=field(column='name', is_id=False, default={'kind': 'literal', 'value': "O'Brien"})),
        {},
        'postgresql',
    )
    # the apostrophe must be escaped or the DDL is a syntax error
    assert server_default(built, 'Thing', 'name') == "'O''Brien'"


@pytest.mark.parametrize(('value', 'expected'), [(True, 'true'), (False, 'false')])
def test_boolean_literal_defaults(value: bool, expected: str) -> None:
    """`str(True)` is `'True'`, which PostgreSQL will not accept unquoted."""
    built = build_metadata(
        schema(flag=field(column='flag', type='Boolean', is_id=False, default={'kind': 'literal', 'value': value})),
        {},
        'postgresql',
    )
    assert server_default(built, 'Thing', 'flag') == expected


# -- referential actions ------------------------------------------------------


def relation(**overrides: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        'to': 'Thing',
        'shape': 'to-one-owner',
        'relation_name': 'ThingToThing',
        'is_list': False,
        'nullable': True,
        'owner': True,
        'back_field': None,
        'fk_model': 'Thing',
        'fk_fields': ['parentId'],
        'fk_columns': ['parentId'],
        'referenced_fields': ['id'],
        'referenced_columns': ['id'],
        'fk_required': False,
        'on_delete': None,
        'on_update': None,
        'relation_mode': None,
        'fk_name': 'Thing_parentId_fkey',
        'join_ambiguous': False,
    }
    base.update(overrides)
    return base


def self_relation_schema(**relation_overrides: Any) -> Dict[str, Any]:
    return {
        'Thing': model(
            {'id': field(), 'parentId': field(column='parentId', is_id=False, nullable=True)},
            relations={'parent': relation(**relation_overrides)},
        )
    }


@pytest.mark.parametrize(
    ('declared', 'expected'),
    [
        ('Cascade', 'CASCADE'),
        ('Restrict', 'RESTRICT'),
        ('NoAction', 'NO ACTION'),
        ('SetNull', 'SET NULL'),
        ('SetDefault', 'SET DEFAULT'),
    ],
)
def test_every_prisma_referential_action_is_mapped(declared: str, expected: str) -> None:
    built = build_metadata(self_relation_schema(on_delete=declared), {}, 'postgresql')
    (fk,) = [c for c in built.tables['Thing'].constraints if isinstance(c, sa.ForeignKeyConstraint)]
    assert fk.ondelete == expected


def test_unknown_referential_action_refuses() -> None:
    with pytest.raises(NotImplementedError, match='Obliterate'):
        build_metadata(self_relation_schema(on_delete='Obliterate'), {}, 'postgresql')


def test_undeclared_action_depends_on_nullability() -> None:
    """The single most consequential default in the whole translation."""
    optional = build_metadata(self_relation_schema(fk_required=False), {}, 'postgresql')
    required = build_metadata(self_relation_schema(fk_required=True), {}, 'postgresql')

    def ondelete(built: Any) -> str:
        (fk,) = [c for c in built.tables['Thing'].constraints if hasattr(c, 'ondelete')]
        return str(fk.ondelete)

    assert ondelete(optional) == 'SET NULL'
    assert ondelete(required) == 'RESTRICT'


def only_fk(built: Any) -> Any:
    (constraint,) = [c for c in built.tables['Thing'].constraints if isinstance(c, sa.ForeignKeyConstraint)]
    return constraint


@pytest.mark.parametrize(
    ('declared', 'expected'),
    [
        ('Cascade', 'CASCADE'),
        ('Restrict', 'RESTRICT'),
        ('NoAction', 'NO ACTION'),
        ('SetNull', 'SET NULL'),
        ('SetDefault', 'SET DEFAULT'),
    ],
)
def test_every_prisma_referential_action_is_mapped_for_on_update(declared: str, expected: str) -> None:
    built = build_metadata(self_relation_schema(on_update=declared), {}, 'postgresql')
    assert only_fk(built).onupdate == expected


def test_unknown_on_update_action_refuses() -> None:
    with pytest.raises(NotImplementedError, match='Obliterate'):
        build_metadata(self_relation_schema(on_update='Obliterate'), {}, 'postgresql')


def test_undeclared_on_update_does_not_depend_on_nullability() -> None:
    """Deliberately unlike `on_delete`: Prisma's `onUpdate` default is Cascade throughout.

    Which is exactly why hardcoding CASCADE survived — it is the right answer
    everywhere except the schemas that say otherwise.
    """
    optional = build_metadata(self_relation_schema(fk_required=False), {}, 'postgresql')
    required = build_metadata(self_relation_schema(fk_required=True), {}, 'postgresql')

    assert only_fk(optional).onupdate == 'CASCADE'
    assert only_fk(required).onupdate == 'CASCADE'


def test_metadata_without_on_update_still_builds() -> None:
    """A client generated before `on_update` existed has no such key.

    Retro-compatibility: the whole point of `None` meaning "Prisma's default" is
    that a missing key means the same thing, so an older `metadata.SCHEMA` keeps
    building — and keeps producing the DDL it produced before.
    """
    schema = self_relation_schema()
    del schema['Thing']['relations']['parent']['on_update']

    assert only_fk(build_metadata(schema, {}, 'postgresql')).onupdate == 'CASCADE'


# -- relationMode --------------------------------------------------------------


def foreign_keys(built: Any) -> List[Any]:
    return [c for c in built.tables['Thing'].constraints if isinstance(c, sa.ForeignKeyConstraint)]


@pytest.mark.parametrize('declared', [None, 'foreignKeys'])
def test_relation_mode_with_foreign_keys_emits_the_constraint(declared: Any) -> None:
    """`None` is "the datasource did not say", which is Prisma's default."""
    built = build_metadata(self_relation_schema(relation_mode=declared), {}, 'postgresql')
    assert [str(c.name) for c in foreign_keys(built)] == ['Thing_parentId_fkey']


def test_relation_mode_prisma_emits_no_constraint() -> None:
    """Prisma creates none, so a constraint here diffs against every table.

    And nothing stands in for it: Prisma leaves the relation scalar unindexed
    and only warns, so an index emitted in the constraint's place is a diff of
    its own.
    """
    built = build_metadata(self_relation_schema(relation_mode='prisma'), {}, 'postgresql')
    assert foreign_keys(built) == []
    assert [index.name for index in built.tables['Thing'].indexes] == []
    # the table and its columns are otherwise untouched
    assert sorted(built.tables['Thing'].c.keys()) == ['id', 'parentId']


def test_unknown_relation_mode_refuses() -> None:
    """Whether the database has foreign keys at all is not something to guess."""
    with pytest.raises(NotImplementedError, match='emulated'):
        build_metadata(self_relation_schema(relation_mode='emulated'), {}, 'postgresql')


def test_metadata_without_relation_mode_still_builds() -> None:
    """A client generated before `relation_mode` existed has no such key.

    Retro-compatibility, and specifically byte-identical DDL: the absent key has
    to mean `foreignKeys`, or upgrading `prisma.sa` without regenerating drops
    every constraint in the schema.
    """
    schema = self_relation_schema()
    del schema['Thing']['relations']['parent']['relation_mode']

    assert [str(c.name) for c in foreign_keys(build_metadata(schema, {}, 'postgresql'))] == ['Thing_parentId_fkey']


# -- schemas the builder should handle without special-casing ------------------


def test_model_with_no_primary_key() -> None:
    """Prisma permits a model identified only by a unique constraint."""
    built = build_metadata(
        {
            'Thing': model(
                {'email': field(column='email', is_id=False)},
                primary_key={'name': None, 'db_name': None, 'fields': [], 'columns': []},
                uniques=[
                    {
                        'name': 'email',
                        'db_name': 'Thing_email_key',
                        'fields': ['email'],
                        'columns': ['email'],
                        'is_defined_on_field': True,
                    }
                ],
            )
        },
        {},
        'postgresql',
    )
    table = built.tables['Thing']
    assert not table.primary_key.columns
    assert [i.name for i in table.indexes] == ['Thing_email_key']


def test_empty_schema_builds() -> None:
    """A schema with no models is legal, if useless."""
    assert build_metadata({}, {}, 'postgresql').tables == {}


# -- native type arguments ----------------------------------------------------
#
# Found by pyright, not by these tests: `sa.Time` takes no `precision`, so
# `@db.Time(6)` raised `TypeError` at runtime. The reference schema has no
# `@db.Time`, so nothing exercised it — the same "a fixture only tests what it
# contains" gap that produced the original five defects. Every `@db.*` that
# accepts arguments is now constructed here.


#: (annotation, args, rendered). One entry per `@db.*` that takes arguments or
#: whose rendering is not obvious; the guard below keeps this in step with the
#: table it tests.
NATIVE_TYPE_CASES = [
    ('Uuid', [], 'UUID'),
    ('Text', [], 'TEXT'),
    ('VarChar', ['40'], 'VARCHAR(40)'),
    ('VarChar', [], 'TEXT'),
    ('Char', ['8'], 'CHAR(8)'),
    ('Bit', ['3'], 'BIT(3)'),
    ('VarBit', ['3'], 'BIT VARYING(3)'),
    ('SmallInt', [], 'SMALLINT'),
    ('Integer', [], 'INTEGER'),
    ('BigInt', [], 'BIGINT'),
    ('Real', [], 'REAL'),
    ('DoublePrecision', [], 'DOUBLE PRECISION'),
    ('Decimal', ['12', '2'], 'NUMERIC(12, 2)'),
    ('Decimal', [], 'NUMERIC'),
    ('Date', [], 'DATE'),
    ('Time', ['6'], 'TIME(6) WITHOUT TIME ZONE'),
    ('Time', [], 'TIME WITHOUT TIME ZONE'),
    ('Timetz', ['6'], 'TIME(6) WITH TIME ZONE'),
    ('Timestamp', ['3'], 'TIMESTAMP(3) WITHOUT TIME ZONE'),
    ('Timestamptz', ['6'], 'TIMESTAMP(6) WITH TIME ZONE'),
    ('ByteA', [], 'BYTEA'),
    ('Json', [], 'JSON'),
    ('JsonB', [], 'JSONB'),
    ('Inet', [], 'INET'),
    ('Boolean', [], 'BOOLEAN'),
    ('Oid', [], 'OID'),
    ('Money', [], 'MONEY'),
    ('Citext', [], 'TEXT'),
]


@pytest.mark.parametrize(('annotation', 'args', 'rendered'), NATIVE_TYPE_CASES)
def test_every_native_type_constructs(annotation: str, args: List[str], rendered: str) -> None:
    """Constructing it is the point — an unbuildable type is a TypeError, not a diff."""
    from sqlalchemy.dialects import postgresql

    from prisma.sa._types import native_type

    type_ = native_type('postgresql', annotation, args)
    # SQLAlchemy leaves `PGDialect_psycopg2.__init__` unannotated, so
    # constructing the dialect is an untyped call as far as mypy is concerned.
    dialect = postgresql.dialect()  # type: ignore[no-untyped-call]
    assert str(type_.compile(dialect=dialect)) == rendered


def test_every_mapped_native_type_is_constructed_somewhere() -> None:
    """Adding a `@db.*` entry without a case here is how `@db.Time(6)` shipped broken."""
    from prisma.sa._types import _PG_NATIVE

    covered = {case[0] for case in NATIVE_TYPE_CASES}
    missing = set(_PG_NATIVE) - covered
    assert missing == {'Xml'}, f'unconstructed @db.* annotations: {sorted(missing)}'
