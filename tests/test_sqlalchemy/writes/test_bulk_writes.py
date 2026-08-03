"""What Prisma's bulk writes do, checked against Prisma.

`create_many`, `update_many` and `delete_many` are the three set-based writes.
All three return a **count** rather than rows, and all three interact with the
client-side values in `prisma.sa._values` in ways that only show up when you
compare two rows of the same batch against each other:

* `create_many` gives every row its **own** id and the whole batch **one**
  instant. Generating a fresh timestamp per row looks identical in a single-row
  test and diverges from Prisma the moment there are two.
* `update_many` stamps `@updatedAt` on every **matched** row, including rows
  whose values did not actually change, and stamps them all with one instant.

Each test here runs the Prisma call, records what happened, clears the tables,
runs the SQLAlchemy translation, and compares. Timestamps are never compared
between the two passes — they happen at different instants — only the *shape*
they produce: how many distinct values a batch left behind, and whether a row's
stamp moved past where it was seeded.

The live harness for the same translations is
`benchmarks/pg-lab/verify_bulk_writes.py`, which runs them against the 41-model
lab schema.
"""

from __future__ import annotations

import decimal
import datetime
import itertools
from typing import Any, Dict, List, Mapping, Sequence

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from prisma.sa import values_for_create, values_for_update

#: A `createdAt`/`updatedAt` far enough in the past that "did this move?" is
#: answerable without comparing two live clocks.
SEEDED = datetime.datetime(2020, 1, 1, 12, 0, 0)


# ---------------------------------------------------------------------------
# the translations under test
# ---------------------------------------------------------------------------


def insert_many(
    conn: 'sa.Connection',
    table: 'sa.Table',
    model: str,
    data: Sequence[Mapping[str, Any]],
    *,
    skip_duplicates: bool = False,
) -> int:
    """`db.<model>.create_many(data=..., skip_duplicates=...)`.

    Three things here are the translation and not incidental:

    * **one `moment` for the whole batch**, because that is what Prisma does;
    * **one INSERT per distinct key set**, because a single `VALUES` clause is
      compiled from the *first* mapping and silently drops keys that only later
      rows have (`test_a_single_values_clause_drops_keys_that_only_later_rows_have`);
    * **the count comes from `RETURNING`**, because `CursorResult.rowcount` is
      `-1` for an INSERT under psycopg and cannot be the source of the count.
    """
    moment = datetime.datetime.now(datetime.timezone.utc)
    rows = [values_for_create(model, item, moment=moment) for item in data]

    inserted = 0
    for _, group in itertools.groupby(sorted(rows, key=lambda row: sorted(row)), key=lambda row: tuple(sorted(row))):
        batch = list(group)
        statement: sa.Insert
        if skip_duplicates:
            statement = postgresql.insert(table).values(batch).on_conflict_do_nothing()
        else:
            statement = sa.insert(table).values(batch)
        inserted += len(conn.execute(statement.returning(*table.primary_key.columns)).fetchall())
    return inserted


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def clear(engine: 'sa.Engine', *tables: str) -> None:
    with engine.connect() as conn:
        conn.execute(sa.text('TRUNCATE ' + ', '.join(f'"{table}"' for table in tables) + ' RESTART IDENTITY CASCADE'))
        conn.commit()


def account_data(*slugs: str) -> List[Dict[str, Any]]:
    return [{'email': f'{slug}@example.com', 'slug': slug, 'balance': decimal.Decimal('1.5')} for slug in slugs]


def account_batch_shape(engine: 'sa.Engine') -> Dict[str, Any]:
    """The two claims about a `create_many` batch: own id, shared instant."""
    with engine.connect() as conn:
        rows = conn.execute(sa.text('SELECT id, url_slug, "createdAt", "updatedAt" FROM accounts')).mappings().all()
    return {
        'rows': len(rows),
        'slugs': sorted(row['url_slug'] for row in rows),
        'distinct ids': len({row['id'] for row in rows}),
        'distinct createdAt': len({row['createdAt'] for row in rows}),
        'distinct updatedAt': len({row['updatedAt'] for row in rows}),
        'createdAt == updatedAt': all(row['createdAt'] == row['updatedAt'] for row in rows),
    }


def account_stamp_shape(engine: 'sa.Engine') -> Dict[str, Any]:
    with engine.connect() as conn:
        rows = (
            conn.execute(sa.text('SELECT url_slug, role::text AS role, "createdAt", "updatedAt" FROM accounts'))
            .mappings()
            .all()
        )
    return {
        'rows': len(rows),
        'roles': sorted(row['role'] for row in rows),
        'distinct updatedAt': len({row['updatedAt'] for row in rows}),
        'createdAt untouched': sorted({row['createdAt'] for row in rows}) == [SEEDED],
        'updatedAt moved': sum(1 for row in rows if row['updatedAt'] > SEEDED),
    }


def seed_accounts(engine: 'sa.Engine', accounts: 'sa.Table', specs: Sequence[Sequence[str]]) -> None:
    """`(slug, role)` rows with a known, fixed `createdAt`/`updatedAt`.

    Seeded through `values_for_create` rather than raw SQL so that `ADMIN`
    reaches the column as `administrator` — the `@map`ped label, which is the
    only thing the enum type accepts.
    """
    with engine.connect() as conn:
        for slug, role in specs:
            conn.execute(
                sa.insert(accounts).values(
                    **values_for_create(
                        'Account',
                        {
                            'email': f'{slug}@example.com',
                            'slug': slug,
                            'role': role,
                            'balance': decimal.Decimal(0),
                        },
                        moment=SEEDED,
                    )
                )
            )
        conn.commit()


def refused(call: Any) -> Any:
    """`('raised',)` rather than the exception class.

    Prisma raises `UniqueViolationError` and SQLAlchemy raises `IntegrityError`.
    The claim under test is not that the names match, it is that both refuse the
    batch and that neither leaves half of it behind.
    """
    try:
        return ('returned', call())
    except Exception:  # noqa: BLE001 - the type is deliberately not compared
        return ('raised',)


# ---------------------------------------------------------------------------
# offline: the contract `moment` exists for
# ---------------------------------------------------------------------------


def test_one_moment_across_a_batch_shares_the_timestamp_but_not_the_id(installed: None) -> None:
    """Why `values_for_create` takes a `moment` at all.

    Called once per row without one, each row gets its own reading of the clock
    — which is a divergence from Prisma that is invisible in any single-row test
    and only shows up when two rows of the same batch are compared.
    """
    moment = datetime.datetime(2023, 5, 4, 3, 2, 1, 123000, tzinfo=datetime.timezone.utc)
    rows = [values_for_create('Account', item, moment=moment) for item in account_data('a', 'b', 'c')]

    assert len({row['createdAt'] for row in rows}) == 1
    assert len({row['updatedAt'] for row in rows}) == 1
    assert {row['createdAt'] for row in rows} == {row['updatedAt'] for row in rows}
    assert len({row['id'] for row in rows}) == 3, 'one instant, but never one id'


def test_values_for_update_refreshes_updated_at_for_the_whole_matched_set(installed: None) -> None:
    """`update_many` is one statement, so one stamp covers every matched row."""
    moment = datetime.datetime(2023, 5, 4, 3, 2, 1, tzinfo=datetime.timezone.utc)
    values = values_for_update('Account', {'slug': 'moved'}, moment=moment)

    assert values == {'url_slug': 'moved', 'updatedAt': moment.replace(tzinfo=None)}
    assert 'createdAt' not in values, 'an update does not re-run @default(now())'
    assert 'id' not in values, 'nor @default(cuid())'


# ---------------------------------------------------------------------------
# create_many
# ---------------------------------------------------------------------------


def test_create_many_returns_a_count_and_so_does_our_insert(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    write_metadata: 'sa.MetaData',
    installed_write_schema: None,
) -> None:
    theirs = prisma_client.account.create_many(data=account_data('a', 'b', 'c', 'd'))
    assert isinstance(theirs, int), 'a count, not rows'
    theirs_shape = account_batch_shape(write_engine)

    clear(write_engine, 'accounts')

    accounts = write_metadata.tables['accounts']
    with write_engine.connect() as conn:
        ours = insert_many(conn, accounts, 'Account', account_data('a', 'b', 'c', 'd'))
        conn.commit()

    assert ours == theirs == 4
    assert account_batch_shape(write_engine) == theirs_shape


def test_every_row_in_a_batch_gets_its_own_id_and_the_batch_one_instant(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    write_metadata: 'sa.MetaData',
    installed_write_schema: None,
) -> None:
    """The measurement `moment` was added for, made against the engine."""
    prisma_client.account.create_many(data=account_data('a', 'b', 'c', 'd', 'e'))
    theirs = account_batch_shape(write_engine)

    assert theirs['distinct ids'] == 5, 'Prisma gives every row in a batch its own id'
    assert theirs['distinct createdAt'] == 1, 'and the whole batch a single instant'
    assert theirs['createdAt == updatedAt']

    clear(write_engine, 'accounts')

    accounts = write_metadata.tables['accounts']
    with write_engine.connect() as conn:
        insert_many(conn, accounts, 'Account', account_data('a', 'b', 'c', 'd', 'e'))
        conn.commit()

    assert account_batch_shape(write_engine) == theirs


def test_create_many_leaves_the_database_side_defaults_to_the_database(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    write_metadata: 'sa.MetaData',
    installed_write_schema: None,
) -> None:
    """`autoincrement()` and a literal default are the database's job, in a
    batch exactly as in a single row — and `@map` still applies."""
    prisma_client.label.create_many(data=[{'name': 'one'}, {'name': 'two'}])
    theirs = sorted((row.id, row.name) for row in prisma_client.label.find_many())

    clear(write_engine, 'labels')

    labels = write_metadata.tables['labels']
    with write_engine.connect() as conn:
        values = [values_for_create('Label', {'name': name}) for name in ('one', 'two')]
        assert all(list(row) == ['label_name'] for row in values), 'no id: the sequence supplies it'
        assert insert_many(conn, labels, 'Label', [{'name': 'one'}, {'name': 'two'}]) == 2
        conn.commit()

    assert sorted((row.id, row.name) for row in prisma_client.label.find_many()) == theirs


def test_skip_duplicates_is_on_conflict_do_nothing(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    write_metadata: 'sa.MetaData',
    installed_write_schema: None,
) -> None:
    """The count is rows *inserted*, not rows offered."""
    prisma_client.account.create_many(data=account_data('a'))
    theirs = prisma_client.account.create_many(data=account_data('a', 'b', 'c'), skip_duplicates=True)
    theirs_slugs = sorted(row.slug for row in prisma_client.account.find_many())

    clear(write_engine, 'accounts')

    accounts = write_metadata.tables['accounts']
    with write_engine.connect() as conn:
        insert_many(conn, accounts, 'Account', account_data('a'))
        ours = insert_many(conn, accounts, 'Account', account_data('a', 'b', 'c'), skip_duplicates=True)
        conn.commit()

    assert ours == theirs == 2
    assert sorted(row.slug for row in prisma_client.account.find_many()) == theirs_slugs == ['a', 'b', 'c']


def test_a_duplicate_inside_the_batch_is_skipped_too(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    write_metadata: 'sa.MetaData',
    installed_write_schema: None,
) -> None:
    """Nothing pre-exists here — the two `a` rows conflict with each other.

    PostgreSQL resolves that inside the one statement, so `ON CONFLICT DO
    NOTHING` reproduces it without the caller de-duplicating first.
    """
    theirs = prisma_client.account.create_many(data=account_data('a', 'a', 'b'), skip_duplicates=True)

    clear(write_engine, 'accounts')

    accounts = write_metadata.tables['accounts']
    with write_engine.connect() as conn:
        ours = insert_many(conn, accounts, 'Account', account_data('a', 'a', 'b'), skip_duplicates=True)
        conn.commit()

    assert ours == theirs == 2
    assert sorted(row.slug for row in prisma_client.account.find_many()) == ['a', 'b']


def test_without_skip_duplicates_the_conflict_aborts_the_whole_batch(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    write_metadata: 'sa.MetaData',
    installed_write_schema: None,
) -> None:
    prisma_client.account.create_many(data=account_data('a'))
    theirs = refused(lambda: prisma_client.account.create_many(data=account_data('a', 'b', 'c')))
    theirs_slugs = sorted(row.slug for row in prisma_client.account.find_many())

    clear(write_engine, 'accounts')

    accounts = write_metadata.tables['accounts']
    with write_engine.connect() as conn:
        insert_many(conn, accounts, 'Account', account_data('a'))
        conn.commit()
    with write_engine.connect() as conn:
        ours = refused(lambda: insert_many(conn, accounts, 'Account', account_data('a', 'b', 'c')))
        conn.commit()

    assert ours == theirs == ('raised',)
    assert sorted(row.slug for row in prisma_client.account.find_many()) == theirs_slugs == ['a']


def test_a_single_values_clause_drops_keys_that_only_later_rows_have(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    write_metadata: 'sa.MetaData',
    installed_write_schema: None,
) -> None:
    """The trap `insert_many` groups by key set to avoid.

    `sa.insert(t).values([...])` compiles its `VALUES` clause from the **first**
    mapping. A key only present in a later row is dropped with no error at all,
    so the row lands with the column's default instead of the value asked for —
    which for `role` here is `VIEWER` rather than `ADMIN`. The same is true of
    `conn.execute(sa.insert(t), [...])`.
    """
    rows = [
        {'email': 'a@example.com', 'slug': 'a', 'balance': decimal.Decimal(0)},
        {'email': 'b@example.com', 'slug': 'b', 'balance': decimal.Decimal(0), 'role': 'ADMIN'},
    ]
    prisma_client.account.create_many(data=rows)
    theirs = {row.slug: row.role for row in prisma_client.account.find_many()}
    assert theirs == {'a': 'VIEWER', 'b': 'ADMIN'}

    accounts = write_metadata.tables['accounts']

    clear(write_engine, 'accounts')
    with write_engine.connect() as conn:
        moment = datetime.datetime.now(datetime.timezone.utc)
        values = [values_for_create('Account', item, moment=moment) for item in rows]
        conn.execute(sa.insert(accounts).values(values))
        conn.commit()

    wrong = {row.slug: row.role for row in prisma_client.account.find_many()}
    assert wrong == {'a': 'VIEWER', 'b': 'VIEWER'}, 'silently, with no error and the right row count'
    assert wrong != theirs

    clear(write_engine, 'accounts')
    with write_engine.connect() as conn:
        assert insert_many(conn, accounts, 'Account', rows) == 2
        conn.commit()

    assert {row.slug: row.role for row in prisma_client.account.find_many()} == theirs


# ---------------------------------------------------------------------------
# update_many
# ---------------------------------------------------------------------------


def test_update_many_counts_matched_rows_and_stamps_every_one_of_them(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    write_metadata: 'sa.MetaData',
    installed_write_schema: None,
) -> None:
    """Two rows already hold the value being written.

    They are still matched, still counted, and still get a fresh `@updatedAt` —
    the count is *matched*, not *changed*.
    """
    specs = [('a', 'ADMIN'), ('b', 'ADMIN'), ('c', 'VIEWER'), ('d', 'VIEWER')]
    seed_accounts(write_engine, write_metadata.tables['accounts'], specs)
    theirs = prisma_client.account.update_many(where={'slug': {'in': ['a', 'b', 'c', 'd']}}, data={'role': 'ADMIN'})
    theirs_shape = account_stamp_shape(write_engine)

    assert theirs == 4, 'a and b already held ADMIN and are still counted'
    assert theirs_shape['updatedAt moved'] == 4, 'including the two that did not change'
    assert theirs_shape['distinct updatedAt'] == 1, 'one statement, one instant'
    assert theirs_shape['createdAt untouched']

    clear(write_engine, 'accounts')
    seed_accounts(write_engine, write_metadata.tables['accounts'], specs)

    accounts = write_metadata.tables['accounts']
    with write_engine.connect() as conn:
        ours = conn.execute(
            sa.update(accounts)
            .where(accounts.c.url_slug.in_(['a', 'b', 'c', 'd']))
            .values(**values_for_update('Account', {'role': 'ADMIN'}))
        ).rowcount
        conn.commit()

    assert ours == theirs
    assert account_stamp_shape(write_engine) == theirs_shape


def test_update_many_with_a_compound_where(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    write_metadata: 'sa.MetaData',
    installed_write_schema: None,
) -> None:
    """The §4.1 where-operators, reused unchanged on a set-based write."""
    from prisma._schema import enum_label

    specs = [('alpha', 'VIEWER'), ('alps', 'ADMIN'), ('beta', 'VIEWER'), ('gamma', 'VIEWER')]
    seed_accounts(write_engine, write_metadata.tables['accounts'], specs)
    theirs = prisma_client.account.update_many(
        where={'AND': [{'slug': {'startsWith': 'al'}}, {'role': 'VIEWER'}]},
        data={'role': 'OWNER'},
    )
    theirs_shape = account_stamp_shape(write_engine)
    assert theirs == 1

    clear(write_engine, 'accounts')
    seed_accounts(write_engine, write_metadata.tables['accounts'], specs)

    accounts = write_metadata.tables['accounts']
    with write_engine.connect() as conn:
        ours = conn.execute(
            sa.update(accounts)
            .where(sa.and_(accounts.c.url_slug.like('al%'), accounts.c.role == enum_label('Role', 'VIEWER')))
            .values(**values_for_update('Account', {'role': 'OWNER'}))
        ).rowcount
        conn.commit()

    assert ours == theirs
    assert account_stamp_shape(write_engine) == theirs_shape


def test_update_many_matching_zero_rows_changes_nothing(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    write_metadata: 'sa.MetaData',
    installed_write_schema: None,
) -> None:
    specs = [('a', 'VIEWER'), ('b', 'VIEWER')]
    seed_accounts(write_engine, write_metadata.tables['accounts'], specs)
    theirs = prisma_client.account.update_many(where={'slug': 'absent'}, data={'role': 'ADMIN'})
    theirs_shape = account_stamp_shape(write_engine)

    assert theirs == 0
    assert theirs_shape['updatedAt moved'] == 0, 'no matched row means no stamp anywhere'

    clear(write_engine, 'accounts')
    seed_accounts(write_engine, write_metadata.tables['accounts'], specs)

    accounts = write_metadata.tables['accounts']
    with write_engine.connect() as conn:
        ours = conn.execute(
            sa.update(accounts)
            .where(accounts.c.url_slug == 'absent')
            .values(**values_for_update('Account', {'role': 'ADMIN'}))
        ).rowcount
        conn.commit()

    assert ours == theirs == 0
    assert account_stamp_shape(write_engine) == theirs_shape


# ---------------------------------------------------------------------------
# delete_many
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ('where', 'clause', 'expected'),
    [
        pytest.param({'slug': 'absent'}, 'absent', 0, id='matches-zero'),
        pytest.param({'slug': {'startsWith': 'a'}}, 'a', 2, id='matches-many'),
    ],
)
def test_delete_many_returns_the_number_of_rows_it_removed(
    where: Dict[str, Any],
    clause: str,
    expected: int,
    prisma_client: Any,
    write_engine: 'sa.Engine',
    write_metadata: 'sa.MetaData',
    installed_write_schema: None,
) -> None:
    specs = [('alpha', 'VIEWER'), ('alps', 'VIEWER'), ('beta', 'VIEWER')]
    seed_accounts(write_engine, write_metadata.tables['accounts'], specs)
    theirs = prisma_client.account.delete_many(where=where)
    theirs_slugs = sorted(row.slug for row in prisma_client.account.find_many())

    clear(write_engine, 'accounts')
    seed_accounts(write_engine, write_metadata.tables['accounts'], specs)

    accounts = write_metadata.tables['accounts']
    with write_engine.connect() as conn:
        if clause == 'absent':
            statement = sa.delete(accounts).where(accounts.c.url_slug == 'absent')
        else:
            statement = sa.delete(accounts).where(accounts.c.url_slug.like(f'{clause}%'))
        ours = conn.execute(statement).rowcount
        conn.commit()

    assert ours == theirs == expected
    assert sorted(row.slug for row in prisma_client.account.find_many()) == theirs_slugs


def test_delete_many_with_a_compound_where(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    write_metadata: 'sa.MetaData',
    installed_write_schema: None,
) -> None:
    """Also the one place a `where` on a `@map`ped enum has to be translated.

    Prisma addresses the member by its **schema** name (`ADMIN`); the column
    only holds the mapped label (`administrator`), and a bind of `'ADMIN'` is
    rejected outright by the enum type. `enum_label` is the translation.
    """
    from prisma._schema import enum_label

    assert enum_label('Role', 'ADMIN') == 'administrator'

    specs = [('alpha', 'VIEWER'), ('alps', 'ADMIN'), ('beta', 'VIEWER'), ('gamma', 'VIEWER')]
    seed_accounts(write_engine, write_metadata.tables['accounts'], specs)
    theirs = prisma_client.account.delete_many(
        where={'OR': [{'slug': {'in': ['beta', 'gamma']}}, {'role': 'ADMIN'}]},
    )
    theirs_slugs = sorted(row.slug for row in prisma_client.account.find_many())

    clear(write_engine, 'accounts')
    seed_accounts(write_engine, write_metadata.tables['accounts'], specs)

    accounts = write_metadata.tables['accounts']
    with write_engine.connect() as conn:
        ours = conn.execute(
            sa.delete(accounts).where(
                sa.or_(
                    accounts.c.url_slug.in_(['beta', 'gamma']),
                    accounts.c.role == enum_label('Role', 'ADMIN'),
                )
            )
        ).rowcount
        conn.commit()

    assert ours == theirs == 3
    assert sorted(row.slug for row in prisma_client.account.find_many()) == theirs_slugs == ['alpha']


def test_delete_many_with_no_where_empties_the_table(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    write_metadata: 'sa.MetaData',
    installed_write_schema: None,
) -> None:
    """`where` is optional, and omitting it is not a no-op."""
    specs = [('a', 'VIEWER'), ('b', 'VIEWER'), ('c', 'VIEWER')]
    seed_accounts(write_engine, write_metadata.tables['accounts'], specs)
    theirs = prisma_client.account.delete_many()

    assert theirs == 3
    assert prisma_client.account.find_many() == []

    clear(write_engine, 'accounts')
    seed_accounts(write_engine, write_metadata.tables['accounts'], specs)

    accounts = write_metadata.tables['accounts']
    with write_engine.connect() as conn:
        ours = conn.execute(sa.delete(accounts)).rowcount
        conn.commit()

    assert ours == theirs == 3
    assert prisma_client.account.find_many() == []


# ---------------------------------------------------------------------------
# isolation: what the rollback fixture guarantees for a bulk write
# ---------------------------------------------------------------------------


def test_a_bulk_insert_on_the_rollback_connection_leaves_nothing_behind(
    connection: 'sa.Connection',
    write_engine: 'sa.Engine',
    write_metadata: 'sa.MetaData',
    installed_write_schema: None,
) -> None:
    """A batch that fails part-way must not leave earlier groups committed.

    `insert_many` issues one statement per key set, so under autocommit a
    failure in the second group would keep the first. Inside a transaction — the
    `connection` fixture's, or an explicit `engine.begin()` — the whole batch is
    one unit, which is what Prisma's `create_many` is.
    """
    accounts = write_metadata.tables['accounts']
    assert insert_many(connection, accounts, 'Account', account_data('a', 'b', 'c')) == 3
    assert connection.execute(sa.select(sa.func.count()).select_from(accounts)).scalar_one() == 3

    with write_engine.connect() as other:
        assert other.execute(sa.select(sa.func.count()).select_from(accounts)).scalar_one() == 0
