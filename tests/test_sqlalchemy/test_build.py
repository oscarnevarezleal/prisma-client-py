"""Absolute assertions on the SQLAlchemy metadata we build.

`test_ddl_equivalence.py` proves the whole thing byte-for-byte against a real
`prisma db push`, but it needs a database. These run everywhere and pin the
individual decisions that were wrong before the DDL comparison caught them —
so a regression says *what* broke, not just that something did.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.schema import CreateIndex, CreateTable
from sqlalchemy.dialects import postgresql

from prisma.sa import build_metadata
from prisma.sa._types import UnsupportedProviderError

# SQLAlchemy leaves `PGDialect_psycopg2.__init__` unannotated, so constructing
# the dialect is an untyped call as far as mypy is concerned.
DIALECT: sa.engine.Dialect = postgresql.dialect()  # type: ignore[no-untyped-call]


def table(metadata: sa.MetaData, name: str) -> sa.Table:
    return metadata.tables[name]


def index(metadata: sa.MetaData, table_name: str, name: str) -> sa.Index:
    # a comprehension rather than `next(...)`: the generator never runs to
    # exhaustion, which leaves a partial branch, and the error is worse
    matching = [i for i in table(metadata, table_name).indexes if i.name == name]
    assert matching, f'{table_name} has no index named {name!r}'
    return matching[0]


def index_ddl(metadata: sa.MetaData, table_name: str, name: str) -> str:
    return str(CreateIndex(index(metadata, table_name, name)).compile(dialect=DIALECT))


def operator_classes(metadata: sa.MetaData, table_name: str, name: str) -> Any:
    return index(metadata, table_name, name).dialect_options['postgresql']['ops']


def create_table_ddl(metadata: sa.MetaData, name: str) -> str:
    return str(CreateTable(table(metadata, name)).compile(dialect=DIALECT))


#: `@@map`ped on purpose to a name whose derived `<table>_pkey` overflows 63.
LONG_TABLE = 'compound_key_order_with_a_deliberately_overlong_table_name_here'


# -- tables and columns ------------------------------------------------------


def test_tables_use_mapped_names(metadata: sa.MetaData) -> None:
    assert 'accounts' in metadata.tables  # @@map("accounts")
    assert 'Entry' in metadata.tables  # unmapped
    assert 'Account' not in metadata.tables


def test_columns_use_mapped_names(metadata: sa.MetaData) -> None:
    accounts = table(metadata, 'accounts')
    assert 'url_slug' in accounts.c  # @map("url_slug")
    assert 'slug' not in accounts.c


def test_relation_fields_are_not_columns(metadata: sa.MetaData) -> None:
    entry = table(metadata, 'Entry')
    assert set(entry.c.keys()) == {'id', 'accountId', 'title', 'parentId'}


# -- types -------------------------------------------------------------------


@pytest.mark.parametrize(
    ('column', 'rendered'),
    [
        ('id', 'TEXT'),
        ('balance', 'NUMERIC(65, 30)'),
        # not plain `timestamp` — the precision is what Prisma emits, and
        # getting it wrong is a column alteration on every migration
        ('createdAt', 'TIMESTAMP(3) WITHOUT TIME ZONE'),
        ('payload', 'JSONB'),
        ('secret', 'BYTEA'),
        ('visits', 'BIGINT'),
        ('tags', 'TEXT[]'),
    ],
)
def test_scalar_types(metadata: sa.MetaData, column: str, rendered: str) -> None:
    type_ = table(metadata, 'accounts').c[column].type
    assert str(type_.compile(dialect=DIALECT)) == rendered


def test_enum_column_uses_database_labels(metadata: sa.MetaData) -> None:
    """`@map` on an enum value means the Python name is not the stored label."""
    type_ = table(metadata, 'accounts').c['role'].type
    assert isinstance(type_, postgresql.ENUM)
    assert type_.name == 'Role'
    assert list(type_.enums) == ['OWNER', 'administrator', 'VIEWER']


def test_scalar_lists_are_nullable(metadata: sa.MetaData) -> None:
    """The DMMF reports `tags String[]` as required; the column is not.

    Verified against `prisma db push`. Marking it NOT NULL produces a database
    that rejects rows Prisma would accept.
    """
    assert table(metadata, 'accounts').c['tags'].nullable is True


def test_optional_scalars_are_nullable(metadata: sa.MetaData) -> None:
    accounts = table(metadata, 'accounts')
    assert accounts.c['payload'].nullable is True
    assert accounts.c['email'].nullable is False


# -- defaults ----------------------------------------------------------------


def server_default(metadata: sa.MetaData, table_name: str, column: str) -> Any:
    default = table(metadata, table_name).c[column].server_default
    if default is None:
        return None
    # `.arg` only exists on `DefaultClause`, not on the `FetchedValue` base
    # that `Column.server_default` is declared as.
    assert isinstance(default, sa.DefaultClause)
    return str(default.arg)


def test_client_side_generators_have_no_server_default(metadata: sa.MetaData) -> None:
    """`cuid()`/`uuid()` are filled in by the client and leave no DDL trace.

    A server default here would both diff against every real database and hide
    a missing client-side value behind an INSERT that quietly succeeds.
    """
    assert server_default(metadata, 'accounts', 'id') is None


def test_updated_at_has_no_server_default(metadata: sa.MetaData) -> None:
    assert server_default(metadata, 'accounts', 'updatedAt') is None


def test_now_becomes_current_timestamp(metadata: sa.MetaData) -> None:
    assert server_default(metadata, 'accounts', 'createdAt') == 'CURRENT_TIMESTAMP'


def test_literal_defaults(metadata: sa.MetaData) -> None:
    assert server_default(metadata, 'accounts', 'visits') == '0'
    # the DMMF sends the Python member name; the column stores the mapped label
    assert server_default(metadata, 'accounts', 'role') == '\'VIEWER\'::"Role"'


def test_autoincrement_only_where_declared(metadata: sa.MetaData) -> None:
    """SQLAlchemy's `autoincrement='auto'` would make *any* integer primary key
    a SERIAL; Prisma only does that for `@default(autoincrement())`.
    """
    assert table(metadata, 'labels').c['id'].autoincrement is True
    assert table(metadata, 'Entry').c['id'].autoincrement is False


# -- keys, uniques, indexes --------------------------------------------------


def test_primary_keys(metadata: sa.MetaData) -> None:
    assert [c.name for c in table(metadata, 'accounts').primary_key] == ['id']
    assert [c.name for c in table(metadata, 'Composite').primary_key] == ['left', 'right']


def test_uniques_are_indexes_not_constraints(metadata: sa.MetaData) -> None:
    """Prisma emits `CREATE UNIQUE INDEX`, even on PostgreSQL.

    Alembic tells a unique index and a unique constraint apart, so getting this
    backwards means a drop-and-recreate on every migration.
    """
    accounts = table(metadata, 'accounts')
    assert accounts.constraints == {accounts.primary_key}

    unique = {index.name: sorted(c.name for c in index.columns) for index in accounts.indexes if index.unique}
    assert unique == {
        'accounts_email_key': ['email'],
        'accounts_url_slug_role_key': ['role', 'url_slug'],
    }


def test_compound_primary_key_follows_the_declared_order(metadata: sa.MetaData) -> None:
    """`@@id([right, left])` on a model that declares `left` first.

    Prisma emits `PRIMARY KEY ("right", "left")`. The per-column
    `primary_key=True` flags cannot say that — SQLAlchemy reads the order off the
    column definitions, which are in DMMF field order — so the key was built the
    other way round. It enforces the same uniqueness either way, which is why
    nothing failed; the constraint and its implicit index simply cover the pair
    in the wrong order.
    """
    assert [column.name for column in table(metadata, 'key_order').primary_key] == ['right', 'left']
    assert 'PRIMARY KEY ("right", "left")' in create_table_ddl(metadata, 'key_order')


def test_primary_key_in_field_order_is_left_to_the_server(metadata: sa.MetaData) -> None:
    """Naming it is a separate decision from stating it.

    Where the declared order is the column order, an unnamed constraint compiles
    to the same bare `PRIMARY KEY (...)` PostgreSQL already produces, so nothing
    is named and the emitted models stay free of redundant `__table_args__`.
    """
    composite = table(metadata, 'Composite')
    assert [column.name for column in composite.primary_key] == ['left', 'right']
    assert composite.primary_key.name is None
    assert table(metadata, 'accounts').primary_key.name is None


def test_reordered_primary_key_is_named(metadata: sa.MetaData) -> None:
    """The name is how the order survives into a declarative model.

    `_declarative.py` spells a primary key out exactly when it is named and
    otherwise leaves it to the `primary_key=True` flags — i.e. back to field
    order — so an unnamed constraint here would fix the Core layer and leave the
    generated models describing a different database.
    """
    assert table(metadata, 'key_order').primary_key.name == 'key_order_pkey'


def test_derived_primary_key_name_is_truncated_like_the_server(metadata: sa.MetaData) -> None:
    """63 characters, keeping the suffix and cutting the base.

    Measured against `prisma db push`: Prisma leaves the naming to PostgreSQL,
    and PostgreSQL truncates the base. An untruncated name is not merely a
    different name — SQLAlchemy raises `IdentifierError` before emitting any SQL.
    """
    name = table(metadata, LONG_TABLE).primary_key.name
    assert name == 'compound_key_order_with_a_deliberately_overlong_table_name_pkey'
    assert len(str(name)) == 63


@pytest.mark.parametrize(
    ('base', 'suffix'),
    [
        ('short', '_pkey'),
        ('compound_key_order_with_a_deliberately_overlong_table_name_here', '_pkey'),
        ('a' * 58, '_pkey'),
        ('a' * 59, '_pkey'),
        ('a' * 200, '_key'),
    ],
)
def test_truncation_agrees_with_the_generators_copy(base: str, suffix: str) -> None:
    """Everything derived from a long table name has to land on the same 63 characters.

    `prisma.sa` does not import the generator's Pydantic model tree at runtime,
    so the rule exists twice; two rules that disagree is two constraint names
    for one constraint.
    """
    from prisma.sa._build import _truncate_identifier
    from prisma.generator.models import truncate_identifier

    assert _truncate_identifier(base, suffix) == truncate_identifier(base, suffix)


def test_mapped_primary_key_name_still_wins(generated: dict[str, Any]) -> None:
    """`@@id(map:)` is a declared name and beats the derived one."""
    copied = copy.deepcopy(generated)
    copied['schema']['KeyOrder']['primary_key']['db_name'] = 'key_order_pk'

    built = build_metadata(copied['schema'], copied['enums'], copied['provider'])
    primary_key = built.tables['key_order'].primary_key
    assert primary_key.name == 'key_order_pk'
    assert [column.name for column in primary_key] == ['right', 'left']


def test_plain_indexes(metadata: sa.MetaData) -> None:
    accounts = table(metadata, 'accounts')
    (index,) = [i for i in accounts.indexes if not i.unique]
    assert index.name == 'account_email_created_idx'  # @@index(map: ...)
    assert [c.name for c in index.columns] == ['email', 'createdAt']

    entry = table(metadata, 'Entry')
    (derived,) = [i for i in entry.indexes if not i.unique]
    assert derived.name == 'Entry_accountId_idx'  # Prisma's default name


# -- index field modifiers ---------------------------------------------------
#
# `sort:`, `ops:` and `length:` are per-index-member annotations. Only `sort:`
# was honoured, and only on `@@index`: `@@unique` went straight from its column
# list to the index, so a descending unique member was dropped on both the
# model-level and the field-level form.


def test_raw_operator_class_reaches_the_index(metadata: sa.MetaData) -> None:
    """`ops: raw("text_pattern_ops")` arrives as its literal text.

    Without it the index exists, matches on name, passes an Alembic diff — and
    never serves the `LIKE 'prefix%'` query it was written for, because under a
    non-C collation the default operator class cannot.
    """
    assert operator_classes(metadata, 'catalog_entries', 'catalog_slug_pattern_idx') == {'slug': 'text_pattern_ops'}
    assert index_ddl(metadata, 'catalog_entries', 'catalog_slug_pattern_idx').endswith('(slug text_pattern_ops)')


def test_built_in_operator_class_is_translated(metadata: sa.MetaData) -> None:
    """`ops: JsonbPathOps` is a *Prisma* name; the database has never heard of it.

    Passing it through would emit `USING gin (payload JsonbPathOps)`, which is a
    `CREATE INDEX` failure, so the mapping is measured rather than derived.
    """
    assert operator_classes(metadata, 'catalog_entries', 'catalog_payload_path_idx') == {'payload': 'jsonb_path_ops'}
    ddl = index_ddl(metadata, 'catalog_entries', 'catalog_payload_path_idx')
    assert ddl.endswith('USING gin (payload jsonb_path_ops)')


def test_default_operator_class_is_translated_too(metadata: sa.MetaData) -> None:
    """`ArrayOps` is gin's default for an array column.

    Which means `pg_dump` renders the index identically whether it is emitted or
    dropped — the DDL-equivalence gate cannot see this one at all, and an
    assertion on the built metadata is the only thing that can.
    """
    assert operator_classes(metadata, 'catalog_entries', 'catalog_tags_idx') == {'tags': 'array_ops'}


def test_operator_class_applies_to_one_member_of_a_composite(metadata: sa.MetaData) -> None:
    """...and an explicit `sort: Asc` is not the descending case."""
    assert operator_classes(metadata, 'catalog_entries', 'catalog_title_code_idx') == {'title': 'text_pattern_ops'}
    ddl = index_ddl(metadata, 'catalog_entries', 'catalog_title_code_idx')
    assert ddl.endswith('(title text_pattern_ops, code)')


def test_indexes_without_modifiers_carry_no_operator_classes(metadata: sa.MetaData) -> None:
    """Set only where a modifier asked for it.

    `dialect_kwargs` is what `_declarative.py` renders an index's options from,
    so an unconditional empty dict would put `postgresql_ops={}` on every index
    in every emitted model.
    """
    assert operator_classes(metadata, 'accounts', 'account_email_created_idx') == {}
    assert dict(index(metadata, 'accounts', 'account_email_created_idx').dialect_kwargs) == {}
    assert dict(index(metadata, 'catalog_entries', 'catalog_slug_pattern_idx').dialect_kwargs) == {
        'postgresql_ops': {'slug': 'text_pattern_ops'}
    }


def test_unique_honours_sort_order(metadata: sa.MetaData) -> None:
    """`@@unique([code, seq(sort: Desc)])` is `CREATE UNIQUE INDEX ... (code, seq DESC)`.

    Verified against `prisma db push`. The uniques path used `columns` directly,
    so every modifier on a unique was dropped.
    """
    ddl = index_ddl(metadata, 'catalog_entries', 'catalog_code_seq_key')
    assert ddl.endswith('(code, seq DESC)')
    assert ddl.startswith('CREATE UNIQUE INDEX')


def test_field_level_unique_honours_sort_order(metadata: sa.MetaData) -> None:
    """`ref String @unique(sort: Desc)` — the same wire object, one member.

    There is no `@@unique` line to notice here, which is exactly why this half is
    easy to leave behind.
    """
    assert index_ddl(metadata, 'ledgers', 'ledgers_ref_key').endswith('(ref DESC)')


def test_existing_uniques_are_unaffected(metadata: sa.MetaData) -> None:
    """An unsorted unique still compiles to a plain ascending index."""
    assert index_ddl(metadata, 'accounts', 'accounts_url_slug_role_key').endswith('(url_slug, role)')


# -- foreign keys ------------------------------------------------------------


def fk(metadata: sa.MetaData, table_name: str, name: str) -> sa.ForeignKeyConstraint:
    found = [
        c for c in table(metadata, table_name).constraints if isinstance(c, sa.ForeignKeyConstraint) and c.name == name
    ]
    assert found, f'{table_name} has no foreign key named {name!r}'
    return found[0]


def test_declared_on_delete(metadata: sa.MetaData) -> None:
    constraint = fk(metadata, 'Entry', 'Entry_accountId_fkey')
    assert constraint.ondelete == 'CASCADE'
    assert constraint.onupdate == 'CASCADE'
    assert [e.target_fullname for e in constraint.elements] == ['accounts.id']


def test_undeclared_on_delete_is_restrict_for_required(metadata: sa.MetaData) -> None:
    """Prisma's default for a mandatory relation is RESTRICT.

    Not CASCADE and not NO ACTION — verified against `prisma db push`. Guessing
    CASCADE here turns a rejected delete into a silent cascade of row deletions.
    """
    assert fk(metadata, 'Profile', 'Profile_account_id_fkey').ondelete == 'RESTRICT'


def test_undeclared_on_delete_is_set_null_for_optional(metadata: sa.MetaData) -> None:
    assert fk(metadata, 'Entry', 'Entry_parentId_fkey').ondelete == 'SET NULL'


@pytest.mark.parametrize(
    ('name', 'expected'),
    [
        ('maintenance_locks_restricted_id_fkey', 'RESTRICT'),
        ('maintenance_locks_inert_id_fkey', 'NO ACTION'),
        ('maintenance_lock_nulled_fk', 'SET NULL'),
        ('maintenance_locks_defaulted_id_fkey', 'SET DEFAULT'),
    ],
)
def test_declared_on_update(metadata: sa.MetaData, name: str, expected: str) -> None:
    """Every one of these was `ON UPDATE CASCADE` before, silently.

    `onUpdate` is not in the DMMF at all — a field declaring it sends
    `relationOnDelete` and no `relationOnUpdate` key — so the emitter hardcoded
    Prisma's default and got it right on every schema that never declared one.
    """
    assert fk(metadata, 'maintenance_locks', name).onupdate == expected


def test_undeclared_on_update_is_cascade_at_every_arity(metadata: sa.MetaData) -> None:
    """Unlike `onDelete`, the default does not depend on whether the FK is required.

    Verified against `prisma db push`: mandatory and optional relations alike get
    `ON UPDATE CASCADE`.
    """
    assert fk(metadata, 'maintenance_locks', 'maintenance_locks_plain_id_fkey').onupdate == 'CASCADE'
    # required
    assert fk(metadata, 'Profile', 'Profile_account_id_fkey').onupdate == 'CASCADE'
    # optional
    assert fk(metadata, 'Entry', 'Entry_parentId_fkey').onupdate == 'CASCADE'


def test_on_update_does_not_disturb_on_delete(metadata: sa.MetaData) -> None:
    """The two are separate constraints on the same foreign key."""
    restricted = fk(metadata, 'maintenance_locks', 'maintenance_locks_restricted_id_fkey')
    assert (restricted.onupdate, restricted.ondelete) == ('RESTRICT', 'CASCADE')

    # declares `onUpdate` only, so `onDelete` still takes Prisma's arity default
    inert = fk(metadata, 'maintenance_locks', 'maintenance_locks_inert_id_fkey')
    assert (inert.onupdate, inert.ondelete) == ('NO ACTION', 'RESTRICT')


def test_only_the_owning_side_declares_the_foreign_key(metadata: sa.MetaData) -> None:
    """Both sides of a relation describe the same constraint."""
    accounts = table(metadata, 'accounts')
    assert not [c for c in accounts.constraints if isinstance(c, sa.ForeignKeyConstraint)]


# -- implicit many-to-many ---------------------------------------------------


def test_join_table_shape(metadata: sa.MetaData) -> None:
    join = table(metadata, '_EntryToLabel')

    assert [c.name for c in join.c] == ['A', 'B']
    assert all(not c.nullable for c in join.c)
    # Prisma creates no primary key on the join table
    assert not join.primary_key.columns

    # column types follow the referenced primary keys, which differ here
    assert isinstance(join.c['A'].type, sa.Text)
    assert isinstance(join.c['B'].type, sa.Integer)

    indexes = {index.name: (index.unique, [c.name for c in index.columns]) for index in join.indexes}
    assert indexes == {
        '_EntryToLabel_AB_unique': (True, ['A', 'B']),
        '_EntryToLabel_B_index': (False, ['B']),
    }


def test_join_table_foreign_keys_cascade(metadata: sa.MetaData) -> None:
    for name, target in (('A', 'Entry.id'), ('B', 'labels.id')):
        constraint = fk(metadata, '_EntryToLabel', f'_EntryToLabel_{name}_fkey')
        assert [e.target_fullname for e in constraint.elements] == [target]
        assert constraint.ondelete == 'CASCADE'
        assert constraint.onupdate == 'CASCADE'


def test_join_table_built_once(metadata: sa.MetaData) -> None:
    """Both sides of the relation describe the same table."""
    assert len([name for name in metadata.tables if name.startswith('_EntryToLabel')]) == 1


def test_self_many_to_many_join_table(metadata: sa.MetaData) -> None:
    """The *direction* is ambiguous for a self-relation, but the table is not."""
    join = table(metadata, '_similar')
    assert [c.name for c in join.c] == ['A', 'B']
    for name in ('A', 'B'):
        assert [e.target_fullname for e in fk(metadata, '_similar', f'_similar_{name}_fkey').elements] == ['labels.id']


# -- relationMode = "prisma" -------------------------------------------------
#
# The database then has no foreign keys at all. `test_ddl_equivalence_relation_
# mode.py` proves that against a real `prisma db push`; these pin the individual
# decisions so a regression says which one broke.


def test_relation_mode_prisma_emits_no_foreign_keys(relation_mode_metadata: sa.MetaData) -> None:
    constraints = [
        (name, c.name)
        for name, t in relation_mode_metadata.tables.items()
        for c in t.constraints
        if isinstance(c, sa.ForeignKeyConstraint)
    ]
    assert constraints == []


def test_relation_mode_prisma_leaves_the_join_table_otherwise_intact(
    relation_mode_metadata: sa.MetaData,
) -> None:
    """The join table is the easiest half to forget: it has no model.

    Columns, nullability and both indexes are unchanged; only the two foreign
    keys are gone.
    """
    join = table(relation_mode_metadata, '_GroupToMember')

    assert [c.name for c in join.c] == ['A', 'B']
    assert all(not c.nullable for c in join.c)
    assert not [c for c in join.constraints if isinstance(c, sa.ForeignKeyConstraint)]

    indexes = {index.name: (index.unique, [c.name for c in index.columns]) for index in join.indexes}
    assert indexes == {
        '_GroupToMember_AB_unique': (True, ['A', 'B']),
        '_GroupToMember_B_index': (False, ['B']),
    }


def test_relation_mode_prisma_invents_no_index_for_the_missing_constraint(
    relation_mode_metadata: sa.MetaData,
) -> None:
    """Measured: Prisma creates none and only warns.

    An index emitted here would look like the helpful thing to do and would be a
    diff on every relation scalar in the schema.
    """
    members = table(relation_mode_metadata, 'members')
    indexed = {str(index.name): [c.name for c in index.columns] for index in members.indexes}

    assert [name for name, columns in indexed.items() if columns == ['tenant_id']] == []
    # the declared `@@index([managerId])` is still there, so this is not simply
    # "no indexes on this table"
    assert indexed['member_manager_idx'] == ['managerId']


def test_relation_mode_prisma_keeps_the_rest_of_the_schema(relation_mode_metadata: sa.MetaData) -> None:
    """Only the constraints go. Names, defaults and sequences are untouched."""
    assert sorted(relation_mode_metadata.tables) == [
        '_GroupToMember',
        'configs',
        'groups',
        'members',
        'regions',
        'seats',
        'tenants',
    ]
    tenants = table(relation_mode_metadata, 'tenants')
    assert [c.name for c in tenants.primary_key.columns] == ['id']
    assert isinstance(tenants.c['tier'].type, postgresql.ENUM)


# -- providers ---------------------------------------------------------------


def test_unverified_provider_refuses(generated: dict[str, Any]) -> None:
    """Better a named refusal than a plausible mapping.

    A wrong type table is discovered as a schema diff during someone else's
    deploy; a refusal is discovered immediately.
    """
    with pytest.raises(UnsupportedProviderError) as exc:
        build_metadata(generated['schema'], generated['enums'], 'mysql')

    assert 'mysql' in str(exc.value)
    assert 'postgresql' in str(exc.value)
