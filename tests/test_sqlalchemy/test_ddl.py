"""The DDL we would emit, compiled but not executed.

`test_ddl_equivalence.py` is the real gate — it builds two databases and
requires `pg_dump` to agree — but it needs PostgreSQL, so most runs skip it.
Compiling against the PostgreSQL dialect needs no server, so these assertions
run everywhere and pin the full statement rather than one attribute at a time.

The expected strings were taken from a database built by `prisma db push`, not
written by hand. When one of these changes, check it against a real `db push`
before updating it — a green suite with drifted expectations is worse than a
red one.
"""

from __future__ import annotations

import textwrap
from typing import List

import pytest
import sqlalchemy as sa
from sqlalchemy.schema import CreateIndex, CreateTable
from sqlalchemy.dialects import postgresql

DIALECT = postgresql.dialect()


def _normalize(ddl: str) -> str:
    """Drop SQLAlchemy's trailing spaces after each column line.

    They carry no meaning, no formatter will let them survive in the expected
    strings below, and a SQLAlchemy patch release is free to change them. Every
    other character is compared exactly.
    """
    return '\n'.join(line.rstrip() for line in ddl.strip().splitlines())


def create_table(metadata: sa.MetaData, name: str) -> str:
    return _normalize(str(CreateTable(metadata.tables[name]).compile(dialect=DIALECT)))


def create_indexes(metadata: sa.MetaData, name: str) -> List[str]:
    indexes = sorted(metadata.tables[name].indexes, key=lambda index: index.name or '')
    return [_normalize(str(CreateIndex(index).compile(dialect=DIALECT))) for index in indexes]


def test_scalars_defaults_and_enum(metadata: sa.MetaData) -> None:
    """One statement covering most of the type table at once.

    `role` carries the mapped enum default, `visits` a BigInt literal,
    `createdAt` the `now()` generator, `updatedAt` deliberately nothing, `tags`
    is NULLable despite the DMMF calling the field required, and `id` has no
    default at all because `cuid()` is client-side.
    """
    assert create_table(metadata, 'accounts') == textwrap.dedent("""\
        CREATE TABLE accounts (
        \tid TEXT NOT NULL,
        \temail TEXT NOT NULL,
        \turl_slug TEXT NOT NULL,
        \trole "Role" DEFAULT 'VIEWER'::"Role" NOT NULL,
        \tbalance NUMERIC(65, 30) NOT NULL,
        \tpayload JSONB,
        \tsecret BYTEA,
        \tvisits BIGINT DEFAULT 0 NOT NULL,
        \ttags TEXT[],
        \t"createdAt" TIMESTAMP(3) WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
        \t"updatedAt" TIMESTAMP(3) WITHOUT TIME ZONE NOT NULL,
        \tPRIMARY KEY (id)
        )""")


def test_foreign_key_actions(metadata: sa.MetaData) -> None:
    """A declared CASCADE and an undeclared optional relation side by side.

    The undeclared one is SET NULL — Prisma's default for an optional relation.
    Its required counterpart is RESTRICT, in `Profile` below.
    """
    assert create_table(metadata, 'Entry') == textwrap.dedent("""\
        CREATE TABLE "Entry" (
        \tid TEXT NOT NULL,
        \t"accountId" TEXT NOT NULL,
        \ttitle TEXT NOT NULL,
        \t"parentId" TEXT,
        \tPRIMARY KEY (id),
        \tCONSTRAINT "Entry_accountId_fkey" FOREIGN KEY("accountId") REFERENCES accounts (id) ON DELETE CASCADE ON UPDATE CASCADE,
        \tCONSTRAINT "Entry_parentId_fkey" FOREIGN KEY("parentId") REFERENCES "Entry" (id) ON DELETE SET NULL ON UPDATE CASCADE
        )""")


def test_undeclared_required_relation_is_restrict(metadata: sa.MetaData) -> None:
    assert 'ON DELETE RESTRICT ON UPDATE CASCADE' in create_table(metadata, 'Profile')


def test_compound_primary_key(metadata: sa.MetaData) -> None:
    """`left` and `right` are reserved words; the ordering is the schema's."""
    assert create_table(metadata, 'Composite') == textwrap.dedent("""\
        CREATE TABLE "Composite" (
        \t"left" TEXT NOT NULL,
        \t"right" TEXT NOT NULL,
        \tnote TEXT,
        \tPRIMARY KEY ("left", "right")
        )""")


def test_autoincrement_becomes_serial(metadata: sa.MetaData) -> None:
    assert create_table(metadata, 'labels') == textwrap.dedent("""\
        CREATE TABLE labels (
        \tid SERIAL NOT NULL,
        \tlabel_name TEXT NOT NULL,
        \tPRIMARY KEY (id)
        )""")


def test_non_autoincrement_integer_key_is_not_serial(metadata: sa.MetaData) -> None:
    """The mistake SQLAlchemy's `autoincrement='auto'` would make for us."""
    assert 'SERIAL' not in create_table(metadata, 'Entry')
    assert 'SERIAL' not in create_table(metadata, 'accounts')


def test_join_table(metadata: sa.MetaData) -> None:
    """No primary key, both columns NOT NULL, both FKs fully cascading."""
    assert create_table(metadata, '_EntryToLabel') == textwrap.dedent("""\
        CREATE TABLE "_EntryToLabel" (
        \t"A" TEXT NOT NULL,
        \t"B" INTEGER NOT NULL,
        \tCONSTRAINT "_EntryToLabel_A_fkey" FOREIGN KEY("A") REFERENCES "Entry" (id) ON DELETE CASCADE ON UPDATE CASCADE,
        \tCONSTRAINT "_EntryToLabel_B_fkey" FOREIGN KEY("B") REFERENCES labels (id) ON DELETE CASCADE ON UPDATE CASCADE
        )""")


def test_join_table_indexes(metadata: sa.MetaData) -> None:
    assert create_indexes(metadata, '_EntryToLabel') == [
        'CREATE UNIQUE INDEX "_EntryToLabel_AB_unique" ON "_EntryToLabel" ("A", "B")',
        'CREATE INDEX "_EntryToLabel_B_index" ON "_EntryToLabel" ("B")',
    ]


def test_uniques_compile_as_indexes(metadata: sa.MetaData) -> None:
    """Not `CONSTRAINT ... UNIQUE` — Prisma emits `CREATE UNIQUE INDEX`."""
    assert 'UNIQUE' not in create_table(metadata, 'accounts')
    assert create_indexes(metadata, 'accounts') == [
        'CREATE INDEX account_email_created_idx ON accounts (email, "createdAt")',
        'CREATE UNIQUE INDEX accounts_email_key ON accounts (email)',
        'CREATE UNIQUE INDEX accounts_url_slug_role_key ON accounts (url_slug, role)',
    ]


def test_enum_type_is_created_once(metadata: sa.MetaData) -> None:
    """A shared type, not one per column, and carrying the mapped labels."""
    types = {c.type for t in metadata.tables.values() for c in t.c if isinstance(c.type, postgresql.ENUM)}
    assert len(types) == 1, 'expected exactly one shared enum type'
    assert list(types.pop().enums) == ['OWNER', 'administrator', 'VIEWER']


@pytest.mark.parametrize('table', ['accounts', 'Entry', 'Profile', 'labels', 'Composite', '_EntryToLabel', '_similar'])
def test_every_table_compiles(metadata: sa.MetaData, table: str) -> None:
    """Cheap guard: a table that cannot be compiled cannot be created."""
    assert create_table(metadata, table).startswith('CREATE TABLE')
