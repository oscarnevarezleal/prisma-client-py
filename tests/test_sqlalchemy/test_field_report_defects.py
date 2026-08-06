"""Regressions for defects found by a client running the migration runbook.

Reported against a 182-model production schema after following
`docs/prisma-to-sqlalchemy-runbook.md` as written. Every one produced either a
crash at Phase 2 or — worse, once — a database that passed the runbook's own
Alembic gate and then rejected writes.

B6, B7 and the B3 remainder came from the **retest** of the fix: three more
shapes the reference schema did not contain, found the same way — by running a
real schema against a real database.

B8 was not reported. It was found by writing a schema the reference schema had
no equivalent of, which is the only way any of these get found.

The reference schema in `data/dmmf_wire_sample.prisma` now carries all of them,
so `test_ddl_equivalence.py` covers them against a real `prisma db push` too.
These name them individually so a failure says which defect came back rather
than just that some DDL moved.

The root cause was the same every time: the reference schema exercised none of
these shapes. It was recorded from the pinned CLI, so the DMMF was real — but a
fixture only tests what it contains, and a gate is only as strong as the fixture
behind it.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest
import sqlalchemy as sa
from sqlalchemy.schema import CreateIndex
from sqlalchemy.dialects import postgresql

from prisma.generator.models import MAX_IDENTIFIER_LENGTH, truncate_identifier

# SQLAlchemy leaves `PGDialect_psycopg2.__init__` unannotated, so constructing
# the dialect is an untyped call as far as mypy is concerned.
DIALECT: sa.engine.Dialect = postgresql.dialect()  # type: ignore[no-untyped-call]

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
    default = column.server_default
    assert default is not None, 'no DEFAULT — every INSERT would fail'
    # `Column.server_default` is typed as the `FetchedValue` base, which has no
    # `.arg`; only the `DefaultClause` subclass carries the DDL expression.
    assert isinstance(default, sa.DefaultClause)
    assert 'nextval' in str(default.arg)
    assert 'Ticket_ticketNumber_seq' in str(default.arg)


def test_b5_sequence_is_created_with_the_schema(metadata: sa.MetaData) -> None:
    names = {seq.name for seq in metadata._sequences.values()}
    assert 'Ticket_ticketNumber_seq' in names
    assert 'Ticket_bigNumber_seq' in names


def test_b5_sequence_is_sized_to_the_column(metadata: sa.MetaData) -> None:
    """`CREATE SEQUENCE … AS integer` for `Int`, bigint for `BigInt`.

    SQLAlchemy defaults to bigint, so omitting this makes every `Int` sequence
    a bigint one — a diff on every such column.
    """
    # `_sequences` is keyed by `quoted_name`, so re-key by plain `str` to look
    # sequences up by the literal names below.
    sequences: Dict[str, sa.Sequence] = {str(seq.name): seq for seq in metadata._sequences.values()}
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
    # `.arg` only exists on `DefaultClause`, not on the `FetchedValue` base
    # that `Column.server_default` is declared as.
    assert isinstance(default, sa.DefaultClause)
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
    (unique,) = [u for u in generated['schema']['TicketAttachmentRequirement']['uniques'] if len(u['fields']) == 3]
    assert len(unique['db_name']) == MAX_IDENTIFIER_LENGTH
    assert unique['db_name'].endswith('_key')
    assert unique['db_name'].startswith('ticket_attachment_requirements_')


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
            'attachment_rules_on_attachments_attachmentId_attachmentRuleId_scopeId',
            '_key',
            'attachment_rules_on_attachments_attachmentId_attachmentRule_key',
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
    assert str(type_.compile(dialect=DIALECT)) == rendered


def test_b1_foreign_key_column_matches_the_key_it_references(metadata: sa.MetaData) -> None:
    """A `text` FK pointing at a `uuid` key does not just diff — it will not create."""
    child = metadata.tables['ticket_attachment_requirements'].c['ticketId']
    parent = metadata.tables['Ticket'].c['id']
    assert isinstance(child.type, postgresql.UUID)
    assert type(child.type) is type(parent.type)


def test_b1_unannotated_columns_keep_the_default_mapping(metadata: sa.MetaData) -> None:
    """The lexer must not attach an annotation to the wrong field."""
    assert isinstance(metadata.tables['Ticket'].c['ticketNumber'].type, sa.Integer)
    assert isinstance(metadata.tables['accounts'].c['email'].type, sa.Text)


# -- B3 remainder: foreign key names also overflow ----------------------------


def test_b3_foreign_key_names_are_truncated_too(metadata: sa.MetaData) -> None:
    """The first fix truncated uniques and indexes but not foreign keys.

    The original report's example was a `_key`, it became the test case, and the
    `_fkey` path was never exercised — the same "the fixture only tests what it
    contains" failure, recurring inside its own fix. 7 of the reporter's 10 long
    identifiers were foreign keys and still raised `IdentifierError`.
    """
    constraints = [
        c
        for c in metadata.tables['scheduled_maintenance_window_regions'].constraints
        if isinstance(c, sa.ForeignKeyConstraint)
    ]
    assert constraints, 'scheduled_maintenance_window_regions has no foreign key'
    constraint = constraints[0]
    assert constraint.name == 'scheduled_maintenance_window_regions_scheduled_maintenance_fkey'
    assert len(str(constraint.name)) == MAX_IDENTIFIER_LENGTH


def _index_named(metadata: sa.MetaData, table: str, name: str) -> sa.Index:
    found = [i for i in metadata.tables[table].indexes if i.name == name]
    assert found, f'{table} has no index named {name!r}'
    return found[0]


# -- B6: `sort: Desc` dropped from indexes ------------------------------------


def test_b6_descending_index_column_keeps_its_direction(metadata: sa.MetaData) -> None:
    """`sortOrder` was in the DMMF all along and simply ignored.

    On a composite index this is not cosmetic. `(a ASC, b ASC)` scanned backwards
    yields `(a DESC, b DESC)`, which serves neither `ORDER BY a, b DESC` nor
    `ORDER BY a DESC, b` — so the index the schema asked for cannot be
    substituted by the one that would be built, and those queries fall back to a
    sort node. Silent to the application, visible later as a slow query.
    """
    index = _index_named(metadata, 'regions', 'idx_region_scope_created')
    rendered = str(CreateIndex(index).compile(dialect=DIALECT))
    assert 'created_at DESC' in rendered
    assert 'scope_id, created_at DESC' in rendered


def test_b6_ascending_columns_are_left_alone(metadata: sa.MetaData) -> None:
    index = _index_named(metadata, 'accounts', 'account_email_created_idx')
    assert 'DESC' not in str(CreateIndex(index).compile(dialect=DIALECT))


# -- B7: `@relation(map:)` ignored --------------------------------------------


def test_b7_relation_map_names_the_constraint(metadata: sa.MetaData) -> None:
    """Absent from the DMMF, like `@db.*`, so it has to be lexed.

    Invisible except where `map:` is doing something — which is the only reason
    anyone writes it — and Alembic does not compare constraint names, so the
    gate stays quiet. The same silent class as B5.
    """
    names = {c.name for c in metadata.tables['regions'].constraints if isinstance(c, sa.ForeignKeyConstraint)}
    assert 'custom_region_fk' in names
    assert 'regions_ticket_id_fkey' not in names, 'derived from @@map instead of honouring map:'


def test_b7_unmapped_relations_still_derive_their_name(metadata: sa.MetaData) -> None:
    """The lexer must not attach a name to the wrong relation."""
    names = {c.name for c in metadata.tables['Entry'].constraints if isinstance(c, sa.ForeignKeyConstraint)}
    assert names == {'Entry_accountId_fkey', 'Entry_parentId_fkey'}


# -- B8: `@relation(onUpdate:)` hardcoded to CASCADE --------------------------


def _foreign_keys(metadata: sa.MetaData, table: str) -> Dict[str, sa.ForeignKeyConstraint]:
    return {str(c.name): c for c in metadata.tables[table].constraints if isinstance(c, sa.ForeignKeyConstraint)}


def test_b8_declared_on_update_is_emitted(metadata: sa.MetaData) -> None:
    """The third annotation in the schema language that the DMMF simply drops.

    `@relation(..., onUpdate: Restrict, onDelete: Cascade)` arrives carrying
    `relationOnDelete: 'Cascade'` and **no** `relationOnUpdate` key at all, so
    the emitter wrote `ON UPDATE CASCADE` on every foreign key. Prisma's default
    really is Cascade, which is what made it look correct — and made it wrong
    only on the schemas that bothered to say otherwise.

    Alembic's autogenerate does not compare referential actions, so nothing but
    a `pg_dump` diff would ever have said so.
    """
    actions = {name: fk.onupdate for name, fk in _foreign_keys(metadata, 'maintenance_locks').items()}
    assert actions == {
        'maintenance_locks_restricted_id_fkey': 'RESTRICT',
        'maintenance_locks_inert_id_fkey': 'NO ACTION',
        'maintenance_lock_nulled_fk': 'SET NULL',
        'maintenance_locks_defaulted_id_fkey': 'SET DEFAULT',
        'maintenance_locks_plain_id_fkey': 'CASCADE',
    }


def test_b8_undeclared_relations_keep_prismas_default(metadata: sa.MetaData) -> None:
    """The lexer must not attach an action to the wrong relation.

    Same failure mode as B1 and B7: a per-model, per-field walk that smears one
    field's annotation across its neighbours passes the interesting case and
    breaks every other one.
    """
    for table in ('Entry', 'Profile', 'regions', '_EntryToLabel'):
        for name, fk in _foreign_keys(metadata, table).items():
            assert fk.onupdate == 'CASCADE', name


def test_b8_on_update_survives_alongside_relation_map(metadata: sa.MetaData) -> None:
    """Both lexed `@relation` arguments on one field, in the order `map:` first."""
    nulled = _foreign_keys(metadata, 'maintenance_locks')['maintenance_lock_nulled_fk']
    assert nulled.onupdate == 'SET NULL'
    assert nulled.ondelete == 'SET NULL'  # optional relation, undeclared onDelete
