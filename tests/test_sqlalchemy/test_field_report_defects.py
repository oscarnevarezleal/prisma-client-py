"""Regressions for defects found by a client running the migration runbook.

Reported against a 182-model production schema after following
`docs/prisma-to-sqlalchemy-runbook.md` as written. Every one produced either a
crash at Phase 2 or — worse, once — a database that passed the runbook's own
Alembic gate and then rejected writes.

The reference schema in `data/dmmf_wire_sample.prisma` now carries all five
shapes, so `test_ddl_equivalence.py` covers them against a real `prisma db push`
too. These name them individually so a failure says which defect came back
rather than just that some DDL moved.

The root cause was the same for all five: the reference schema exercised none of
these shapes. It was recorded from the pinned CLI, so the DMMF was real — but a
fixture only tests what it contains.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from prisma.generator.models import MAX_IDENTIFIER_LENGTH, truncate_identifier

# -- B2: `uuid()` arrives as `uuid(4)` ----------------------------------------


def test_b2_versioned_generator_does_not_block_the_schema(generated: Dict[str, Any]) -> None:
    """`NotImplementedError: Unhandled Prisma default generator: uuid(4)()`.

    Prisma normalises `@default(uuid())` to `uuid(4)` on the wire, so the
    generator name never matched the client-side allowlist and `sa.metadata()`
    raised before returning a single table. `@default(uuid())` is the most
    common id strategy in Prisma schemas — on the reporting client this was 176
    of 182 models, i.e. the migration could not start at all.
    """
    default = generated['schema']['Ticket']['fields']['id']['default']
    assert default['name'] == 'uuid'
    assert default['version'] == '4'


def test_b2_uuid_emits_no_server_default(metadata: sa.MetaData) -> None:
    """It is still a client-side generator; the version does not change that."""
    assert metadata.tables['Ticket'].c['id'].server_default is None


# -- B5: non-PK autoincrement loses its sequence ------------------------------


def test_b5_non_pk_autoincrement_keeps_its_sequence(metadata: sa.MetaData) -> None:
    """The only silent, data-rejecting divergence found.

    SQLAlchemy emits SERIAL only for an integer *primary* key. On any other
    column it treats a `Sequence` as a client-side default and emits no DDL
    default, so the column lands NOT NULL with nothing to fill it and every
    INSERT into that table fails.

    Nothing reports it: Alembic's `compare_server_default` is off by default, so
    autogenerate produces an empty diff and the migration passes the runbook's
    own gate.
    """
    column = metadata.tables['Ticket'].c['ticketNumber']
    assert column.server_default is not None, 'no DEFAULT — every INSERT would fail'
    assert 'nextval' in str(column.server_default.arg)
    assert 'Ticket_ticketNumber_seq' in str(column.server_default.arg)


def test_b5_sequence_is_created_with_the_schema(metadata: sa.MetaData) -> None:
    names = {seq.name for seq in metadata._sequences.values()}
    assert 'Ticket_ticketNumber_seq' in names
    assert 'Ticket_bigNumber_seq' in names


def test_b5_sequence_is_sized_to_the_column(metadata: sa.MetaData) -> None:
    """`CREATE SEQUENCE … AS integer` for `Int`, bigint for `BigInt`.

    SQLAlchemy defaults to bigint, so omitting this makes every `Int` sequence
    a bigint one — a diff on every such column.
    """
    sequences = {seq.name: seq for seq in metadata._sequences.values()}
    assert isinstance(sequences['Ticket_ticketNumber_seq'].data_type, sa.Integer)
    assert isinstance(sequences['Ticket_bigNumber_seq'].data_type, sa.BigInteger)


def test_b5_primary_key_autoincrement_still_uses_serial(metadata: sa.MetaData) -> None:
    """A PK must *not* get an explicit sequence — SERIAL already makes one."""
    assert metadata.tables['labels'].c['id'].server_default is None
    assert not any('labels_id' in seq.name for seq in metadata._sequences.values())


# -- B4: empty scalar-list default -------------------------------------------


def server_default(metadata: sa.MetaData, table: str, column: str) -> str:
    default = metadata.tables[table].c[column].server_default
    assert default is not None
    return str(default.arg)


def test_b4_empty_array_default_is_cast(metadata: sa.MetaData) -> None:
    """`psycopg.errors.IndeterminateDatatype: cannot determine type of empty array`.

    A bare `ARRAY[]` is a runtime error, not a diff — `create_all()` aborts on
    the first such table. The cast follows the `@db.*` annotation when there is
    one.
    """
    assert server_default(metadata, 'Ticket', 'readBy') == 'ARRAY[]::uuid[]'


def test_b4_non_empty_array_default_is_also_cast(metadata: sa.MetaData) -> None:
    assert server_default(metadata, 'Ticket', 'keywords') == "ARRAY['urgent', 'billing']::text[]"


# -- B3: identifiers over PostgreSQL's 63-char limit --------------------------


def test_b3_long_constraint_name_is_truncated(generated: Dict[str, Any]) -> None:
    """`sqlalchemy.exc.IdentifierError`, raised before any SQL is sent.

    Prisma truncates and keeps the suffix; the library did not, so Phase 3 could
    not run at all.
    """
    (unique,) = [u for u in generated['schema']['TicketRequirementsOnDocuments']['uniques'] if len(u['fields']) == 3]
    assert len(unique['db_name']) == MAX_IDENTIFIER_LENGTH
    assert unique['db_name'].endswith('_key')
    assert unique['db_name'].startswith('ticket_requirements_on_documents_')


def test_b3_every_emitted_identifier_fits(metadata: sa.MetaData) -> None:
    """Whole-schema guard, not just the one model that reported it."""
    for table in metadata.tables.values():
        assert len(table.name) <= MAX_IDENTIFIER_LENGTH, table.name
        for index in table.indexes:
            assert index.name is not None
            assert len(index.name) <= MAX_IDENTIFIER_LENGTH, index.name
        for constraint in table.constraints:
            # SQLAlchemy uses a sentinel object, not None, for an unnamed
            # constraint; only real strings are ours to keep short
            if isinstance(constraint.name, str):
                assert len(constraint.name) <= MAX_IDENTIFIER_LENGTH, constraint.name
    for sequence in metadata._sequences.values():
        assert len(sequence.name) <= MAX_IDENTIFIER_LENGTH, sequence.name


@pytest.mark.parametrize(
    ('base', 'suffix', 'expected'),
    [
        ('short', '_key', 'short_key'),
        # exactly at the limit, unchanged
        ('a' * 59, '_key', 'a' * 59 + '_key'),
        # one over: the base loses a character, the suffix survives
        ('a' * 60, '_key', 'a' * 59 + '_key'),
        (
            'document_requirements_on_documents_documentId_documentRequirementId_journeyId',
            '_key',
            'document_requirements_on_documents_documentId_documentRequi_key',
        ),
    ],
)
def test_b3_truncation_rule(base: str, suffix: str, expected: str) -> None:
    """The exact name Prisma produced for the reported constraint."""
    assert truncate_identifier(base, suffix) == expected
    assert len(truncate_identifier(base, suffix)) <= MAX_IDENTIFIER_LENGTH


# -- B1: `@db.*` native types -------------------------------------------------


@pytest.mark.parametrize(
    ('column', 'rendered'),
    [
        ('id', 'UUID'),
        ('reference', 'VARCHAR(40)'),
        ('amount', 'NUMERIC(12, 2)'),
        ('seenAt', 'TIMESTAMP(6) WITH TIME ZONE'),
        ('priority', 'SMALLINT'),
        ('readBy', 'UUID[]'),
    ],
)
def test_b1_native_types_are_honoured(metadata: sa.MetaData, column: str, rendered: str) -> None:
    """Prisma sends no `nativeType` key, so these are lexed from the schema text.

    Without that, every one of these reads as the default scalar type — on the
    reporting client, 554 columns including *every* primary and foreign key.
    Correcting a `uuid` -> `text` mistake after cutover is not a diff, it is a
    full-database rewrite with index and FK rebuilds on every table.
    """
    type_ = metadata.tables['Ticket'].c[column].type
    assert str(type_.compile(dialect=postgresql.dialect())) == rendered


def test_b1_foreign_key_column_matches_the_key_it_references(metadata: sa.MetaData) -> None:
    """A `text` FK pointing at a `uuid` key does not just diff — it will not create."""
    child = metadata.tables['ticket_requirements_on_documents'].c['ticketId']
    parent = metadata.tables['Ticket'].c['id']
    assert isinstance(child.type, postgresql.UUID)
    assert type(child.type) is type(parent.type)


def test_b1_unannotated_columns_keep_the_default_mapping(metadata: sa.MetaData) -> None:
    """The lexer must not attach an annotation to the wrong field."""
    assert isinstance(metadata.tables['Ticket'].c['ticketNumber'].type, sa.Integer)
    assert isinstance(metadata.tables['accounts'].c['email'].type, sa.Text)
