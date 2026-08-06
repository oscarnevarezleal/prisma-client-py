"""What Prisma actually does, checked against Prisma.

`test_values.py` pins the shape of the values `prisma.sa` generates. This module
pins the claims those shapes are derived from: it creates a row with the real
client, creates the equivalent row with `values_for_create` and an INSERT, and
compares. A claim that cannot be made here is a claim that has not been checked.
"""

from __future__ import annotations

import re
import uuid as _uuid
import decimal
import datetime
from typing import Any

import pytest
import sqlalchemy as sa

from prisma.sa import values_for_create, values_for_update

CUID = re.compile(r'^c[0-9a-z]{24}$')


def _account_row(prisma_client: Any) -> Any:
    return prisma_client.account.create(
        data={
            'email': 'prisma@example.com',
            'slug': 'prisma',
            'balance': decimal.Decimal('1.5'),
        }
    )


def test_prisma_generates_a_cuid_of_the_shape_we_copy(prisma_client: Any) -> None:
    """The observation `values_for_create` reimplements."""
    ids = [
        prisma_client.account.create(
            data={'email': f'a{n}@example.com', 'slug': f's{n}', 'balance': decimal.Decimal(0)}
        ).id
        for n in range(3)
    ]

    for value in ids:
        assert CUID.match(value), value
    # blocks 2 and 3: a millisecond timestamp and a per-process counter
    now = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
    assert abs(int(ids[0][1:9], 36) - now) < 60_000
    counters = [int(value[9:13], 36) for value in ids]
    assert counters == sorted(counters)
    assert len({value[13:17] for value in ids}) == 1, 'the fingerprint block is per-process'


def test_our_insert_lands_the_same_shape_of_row_as_prisma(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    write_metadata: 'sa.MetaData',
    installed_write_schema: None,
) -> None:
    theirs = _account_row(prisma_client)

    accounts = write_metadata.tables['accounts']
    values = values_for_create(
        'Account',
        {
            'email': 'sqlalchemy@example.com',
            'slug': 'sqlalchemy',
            'balance': decimal.Decimal('1.5'),
            'role': 'ADMIN',
        },
    )
    with write_engine.connect() as conn:
        conn.execute(sa.insert(accounts).values(**values))
        conn.commit()

    ours = prisma_client.account.find_unique(where={'email': 'sqlalchemy@example.com'})
    assert ours is not None, 'the row Prisma reads back is the row we wrote'

    assert CUID.match(ours.id), 'a Prisma-shaped id, not a NULL and not a stray uuid'
    assert len(ours.id) == len(theirs.id)
    assert ours.role == 'ADMIN', 'the mapped label round-trips back to the member name'
    # the columns we deliberately do not fill
    assert ours.visits == 0, 'server default'
    assert ours.tags == [], 'left NULL, presented as [] — exactly what Prisma stores'
    assert ours.createdAt == ours.updatedAt, 'the same instant, as Prisma does it'


def test_the_enum_label_we_write_is_the_one_prisma_writes(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    write_metadata: 'sa.MetaData',
    installed_write_schema: None,
) -> None:
    """`ADMIN @map("administrator")` — the member name never reaches the column."""
    prisma_client.account.create(
        data={'email': 'theirs@example.com', 'slug': 't', 'balance': decimal.Decimal(0), 'role': 'ADMIN'}
    )

    accounts = write_metadata.tables['accounts']
    with write_engine.connect() as conn:
        conn.execute(
            sa.insert(accounts).values(
                **values_for_create(
                    'Account',
                    {'email': 'ours@example.com', 'slug': 'o', 'balance': decimal.Decimal(0), 'role': 'ADMIN'},
                )
            )
        )
        conn.commit()
        rows = conn.execute(sa.text('SELECT email, role::text AS role FROM accounts ORDER BY email')).mappings().all()

    assert [row['email'] for row in rows] == ['ours@example.com', 'theirs@example.com']
    assert [row['role'] for row in rows] == ['administrator', 'administrator']


def test_updated_at_moves_and_created_at_does_not(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    write_metadata: 'sa.MetaData',
    installed_write_schema: None,
) -> None:
    """Both halves of `@updatedAt`, against the engine and against us."""
    row = _account_row(prisma_client)
    assert row.createdAt == row.updatedAt

    theirs = prisma_client.account.update(where={'id': row.id}, data={'slug': 'moved'})
    assert theirs.updatedAt > row.updatedAt
    assert theirs.createdAt == row.createdAt

    accounts = write_metadata.tables['accounts']
    values = values_for_update('Account', {'slug': 'moved-again'})
    assert 'createdAt' not in values

    with write_engine.connect() as conn:
        conn.execute(sa.update(accounts).where(accounts.c.id == row.id).values(**values))
        conn.commit()

    ours = prisma_client.account.find_unique(where={'id': row.id})
    assert ours is not None
    assert ours.updatedAt > theirs.updatedAt
    assert ours.createdAt == row.createdAt


def test_the_uuid_we_generate_is_accepted_by_a_db_uuid_column(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    write_metadata: 'sa.MetaData',
    installed_write_schema: None,
) -> None:
    """`@default(uuid()) @db.Uuid` — a `str` binds to a `uuid` column."""
    tickets = write_metadata.tables['Ticket']
    values = values_for_create(
        'Ticket',
        {
            'reference': 'REF-1',
            'amount': decimal.Decimal('1.25'),
            'seenAt': datetime.datetime.now(datetime.timezone.utc),
            'priority': 1,
        },
    )
    assert _uuid.UUID(values['id']).version == 4

    with write_engine.connect() as conn:
        conn.execute(sa.insert(tickets).values(**values))
        conn.commit()

    ticket = prisma_client.ticket.find_unique(where={'id': values['id']})
    assert ticket is not None
    assert ticket.ticketNumber == 1, 'the sequence filled the column we left alone'
    assert ticket.readBy == []


def test_now_reaches_the_column_even_when_the_server_default_would_not(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    write_metadata: 'sa.MetaData',
    installed_write_schema: None,
) -> None:
    """Why `now()` is filled client-side despite having a server default.

    `CURRENT_TIMESTAMP` is *transaction start* time, so a row inserted late in a
    long transaction would get a `createdAt` from before the work that produced
    it — and a `createdAt` that no longer equals `updatedAt`. Prisma sends the
    value; so do we.
    """
    accounts = write_metadata.tables['accounts']

    with write_engine.connect() as conn:
        started = conn.execute(sa.text('SELECT CURRENT_TIMESTAMP')).scalar_one()
        conn.execute(sa.text('SELECT pg_sleep(0.2)'))
        values = values_for_create(
            'Account', {'email': 'late@example.com', 'slug': 'late', 'balance': decimal.Decimal(0)}
        )
        conn.execute(sa.insert(accounts).values(**values))
        conn.commit()

    row = prisma_client.account.find_unique(where={'email': 'late@example.com'})
    assert row is not None
    assert row.createdAt == row.updatedAt
    assert row.createdAt.replace(tzinfo=None) > started.replace(tzinfo=None)


def test_the_connection_fixture_writes_inside_an_open_transaction(
    connection: 'sa.Connection',
    write_engine: 'sa.Engine',
    write_metadata: 'sa.MetaData',
    installed_write_schema: None,
) -> None:
    """The isolation contract the other write suites are built on.

    A row written on `connection` is invisible to anyone else, which is only
    true while the transaction is open — and an open transaction is what the
    rollback in the fixture teardown discards.
    """
    accounts = write_metadata.tables['accounts']
    connection.execute(
        sa.insert(accounts).values(
            **values_for_create('Account', {'email': 'gone@example.com', 'slug': 'g', 'balance': decimal.Decimal(0)})
        )
    )

    assert connection.execute(sa.select(accounts.c.id).where(accounts.c.email == 'gone@example.com')).first()

    with write_engine.connect() as other:
        assert other.execute(sa.select(accounts.c.id).where(accounts.c.email == 'gone@example.com')).first() is None


@pytest.mark.parametrize('model', ['Account', 'Ticket'])
def test_every_generated_value_is_a_type_the_driver_accepts(
    model: str,
    connection: 'sa.Connection',
    write_metadata: 'sa.MetaData',
    installed_write_schema: None,
) -> None:
    """A value that only looks right until it is bound is not a value."""
    from prisma._schema import model_schema

    values = values_for_create(model, {})
    table = write_metadata.tables[model_schema(model)['table']]

    for column, value in values.items():
        bound = connection.execute(sa.select(sa.literal(value, table.c[column].type))).scalar_one()
        assert bound is not None
