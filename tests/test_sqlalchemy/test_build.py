"""Absolute assertions on the SQLAlchemy metadata we build.

`test_ddl_equivalence.py` proves the whole thing byte-for-byte against a real
`prisma db push`, but it needs a database. These run everywhere and pin the
individual decisions that were wrong before the DDL comparison caught them —
so a regression says *what* broke, not just that something did.
"""

from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from prisma.sa import build_metadata
from prisma.sa._types import UnsupportedProviderError

# SQLAlchemy leaves `PGDialect_psycopg2.__init__` unannotated, so constructing
# the dialect is an untyped call as far as mypy is concerned.
DIALECT: sa.engine.Dialect = postgresql.dialect()  # type: ignore[no-untyped-call]


def table(metadata: sa.MetaData, name: str) -> sa.Table:
    return metadata.tables[name]


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


def test_plain_indexes(metadata: sa.MetaData) -> None:
    accounts = table(metadata, 'accounts')
    (index,) = [i for i in accounts.indexes if not i.unique]
    assert index.name == 'account_email_created_idx'  # @@index(map: ...)
    assert [c.name for c in index.columns] == ['email', 'createdAt']

    entry = table(metadata, 'Entry')
    (derived,) = [i for i in entry.indexes if not i.unique]
    assert derived.name == 'Entry_accountId_idx'  # Prisma's default name


# -- foreign keys ------------------------------------------------------------


def fk(metadata: sa.MetaData, table_name: str, name: str) -> sa.ForeignKeyConstraint:
    return next(
        c for c in table(metadata, table_name).constraints if isinstance(c, sa.ForeignKeyConstraint) and c.name == name
    )


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
