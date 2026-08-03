"""Flat single-row `create` / `update` / `delete`, checked against Prisma.

`test_values_live.py` pins what `values_for_create` puts *in* a row. This module
pins the statements those values go into: the `INSERT`, `UPDATE` and `DELETE`
that §4.1 of the migration runbook tells an agent to write, each one run
alongside the Prisma call it replaces and compared on the row that ends up in
the database.

Scope is deliberately narrow — one row, scalar columns, no nesting and no
relation writes. `connect`/`disconnect`, `*_many`, `upsert` and atomic
operations stay on the STOP list; nothing here should be read as covering them.

Every case here is also a case in `benchmarks/pg-lab/verify_writes.py`, which
runs the same comparisons against the 41-model lab schema. This module is the
copy that runs in CI.

`prisma_client` is requested by tests that never call it: it is the fixture that
truncates the database on setup, so asking for it is how a test says it needs a
clean one. Writes go through `write_engine` rather than `connection` because a
row written on `connection` is inside an uncommitted transaction and the Prisma
client — a separate process — cannot see it.
"""

from __future__ import annotations

import re
import decimal
import datetime
import importlib
from typing import TYPE_CHECKING, Any, Dict, Optional

import pytest

from prisma.sa import values_for_create, values_for_update
from prisma._schema import model_schema

from .conftest import CLIENT_PACKAGE

if TYPE_CHECKING:
    import sqlalchemy as sa

sqlalchemy = pytest.importorskip('sqlalchemy', reason='prisma[sqlalchemy] is not installed')

CUID = re.compile(r'^c[0-9a-z]{24}$')

#: Columns two equivalent rows cannot agree on: the id is generated, and the
#: rest carry the unique values that keep the two rows apart. Everything else is
#: compared literally.
DISTINGUISHING = frozenset({'id', 'email', 'url_slug'})

RECENT = datetime.timedelta(minutes=5)

#: One instant in the two shapes a caller can hand it over in. June, and a zone
#: that observes daylight saving, so a wrong conversion is four hours out.
AWARE = datetime.datetime(2020, 6, 1, 12, 0, tzinfo=datetime.timezone.utc)
NAIVE = AWARE.replace(tzinfo=None)


def account_data(email: str, slug: str, **extra: Any) -> Dict[str, Any]:
    return {'email': email, 'slug': slug, 'balance': decimal.Decimal('1.50'), **extra}


def stamp(value: datetime.datetime) -> Any:
    """A generated timestamp's comparable properties.

    Not the value — two rows are written at two instants. Millisecond exactness
    is in here because Prisma's `DateTime` columns are `timestamp(3)`, so a
    value that kept its microseconds is a value the database rounded behind the
    caller's back.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    reference = value if value.tzinfo is not None else value.replace(tzinfo=datetime.timezone.utc)
    return (
        value.microsecond % 1000 == 0,
        abs(now - reference) < RECENT,
    )


def fingerprint(row: Optional[Any]) -> Any:
    """A row reduced to what two equivalent writes must agree on."""
    if row is None:
        return None

    mapping = dict(row)
    out: Dict[str, Any] = {}
    for key, value in mapping.items():
        if key in DISTINGUISHING:
            out[key] = f'<{key}>'
        elif isinstance(value, datetime.datetime):
            out[key] = stamp(value)
        else:
            out[key] = value

    if 'createdAt' in mapping and 'updatedAt' in mapping:
        out['createdAt == updatedAt'] = mapping['createdAt'] == mapping['updatedAt']
    return out


def read(engine: 'sa.Engine', table: 'sa.Table', where: Any) -> Optional[Dict[str, Any]]:
    with engine.connect() as conn:
        row = conn.execute(sqlalchemy.select(table).where(where)).mappings().first()
    return dict(row) if row is not None else None


@pytest.fixture(name='accounts')
def accounts_fixture(write_metadata: 'sa.MetaData', installed_write_schema: None) -> 'sa.Table':
    return write_metadata.tables['accounts']


# -- create -------------------------------------------------------------------


def test_create_lands_the_row_prisma_lands(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    accounts: 'sa.Table',
) -> None:
    """`db.account.create(data=...)` -> `insert(t).values(**values_for_create(...))`."""
    prisma_client.account.create(data=account_data('theirs@example.com', 'theirs', role='ADMIN'))

    with write_engine.connect() as conn:
        conn.execute(
            sqlalchemy.insert(accounts).values(
                **values_for_create('Account', account_data('ours@example.com', 'ours', role='ADMIN'))
            )
        )
        conn.commit()

    theirs = read(write_engine, accounts, accounts.c.email == 'theirs@example.com')
    ours = read(write_engine, accounts, accounts.c.email == 'ours@example.com')

    assert ours is not None and theirs is not None
    assert fingerprint(ours) == fingerprint(theirs)
    assert CUID.match(ours['id']), 'a Prisma-shaped id, not a NULL and not a stray uuid'


def test_create_returning_is_the_row_that_was_written(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    accounts: 'sa.Table',
) -> None:
    """`create` returns the created row; so does `RETURNING`."""
    with write_engine.connect() as conn:
        returned = (
            conn.execute(
                sqlalchemy.insert(accounts)
                .values(**values_for_create('Account', account_data('ours@example.com', 'ours')))
                .returning(*accounts.c)
            )
            .mappings()
            .one()
        )
        conn.commit()

    stored = read(write_engine, accounts, accounts.c.email == 'ours@example.com')
    assert dict(returned) == stored


def test_the_enum_in_a_returning_row_is_the_stored_label_not_the_member(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    accounts: 'sa.Table',
) -> None:
    """The caveat on `RETURNING`: same row, different Python objects.

    Prisma parses the engine's response into the generated `Enum`; a `RETURNING`
    row is decoded by the column type, which yields the label that is actually
    stored. Under `@map` those are different strings — `ADMIN` against
    `administrator` — and code that compares the two silently stops matching.
    """
    theirs = prisma_client.account.create(data=account_data('theirs@example.com', 'theirs', role='ADMIN'))

    with write_engine.connect() as conn:
        returned = (
            conn.execute(
                sqlalchemy.insert(accounts)
                .values(**values_for_create('Account', account_data('ours@example.com', 'ours', role='ADMIN')))
                .returning(*accounts.c)
            )
            .mappings()
            .one()
        )
        conn.commit()

    assert theirs.role == 'ADMIN', 'Prisma gives the member name back'
    assert returned['role'] == 'administrator', 'RETURNING gives the stored label'
    assert theirs.role != returned['role']


def test_a_returning_row_gives_naive_timestamps_where_prisma_gives_aware(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    accounts: 'sa.Table',
) -> None:
    """The other half of the same caveat, and why they are still the same instant."""
    theirs = prisma_client.account.create(data=account_data('theirs@example.com', 'theirs'))
    stored = read(write_engine, accounts, accounts.c.email == 'theirs@example.com')

    assert stored is not None
    assert theirs.createdAt.tzinfo is not None, 'Prisma returns an aware UTC datetime'
    assert stored['createdAt'].tzinfo is None, '`timestamp without time zone` decodes to naive'
    assert theirs.createdAt.astimezone(datetime.timezone.utc).replace(tzinfo=None) == stored['createdAt']


def test_an_explicit_aware_datetime_is_shifted_by_the_session_timezone(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    accounts: 'sa.Table',
) -> None:
    """Measured, and the reason the runbook says to pass naive UTC.

    `values_for_create` normalises the timestamps it *generates*; a value the
    caller supplies is passed through untouched. PostgreSQL then converts an
    aware datetime into a `timestamp without time zone` column using the
    session's `TimeZone`, which Prisma never does — it sends UTC and gets UTC.
    """
    theirs = prisma_client.account.create(
        data=account_data('theirs@example.com', 'theirs', createdAt=AWARE, updatedAt=AWARE)
    )
    assert theirs.createdAt.astimezone(datetime.timezone.utc) == AWARE

    with write_engine.connect() as conn:
        # `SET TIME ZONE` outlives the transaction and the connection goes back
        # to the pool, so this has to be undone explicitly or every later test
        # in the session inherits it.
        conn.execute(sqlalchemy.text("SET TIME ZONE 'America/New_York'"))
        try:
            for email, moment in (('aware@example.com', AWARE), ('naive@example.com', NAIVE)):
                conn.execute(
                    sqlalchemy.insert(accounts).values(
                        **values_for_create(
                            'Account', account_data(email, email[:5], createdAt=moment, updatedAt=moment)
                        )
                    )
                )
            conn.commit()
        finally:
            conn.execute(sqlalchemy.text('RESET TimeZone'))
            conn.commit()

    stored = read(write_engine, accounts, accounts.c.email == 'theirs@example.com')
    aware = read(write_engine, accounts, accounts.c.email == 'aware@example.com')
    naive = read(write_engine, accounts, accounts.c.email == 'naive@example.com')

    assert stored is not None and aware is not None and naive is not None
    assert naive['createdAt'] == stored['createdAt'], 'naive UTC lands where Prisma lands it'
    assert aware['createdAt'] != stored['createdAt'], 'aware does not'
    assert stored['createdAt'] - aware['createdAt'] == datetime.timedelta(hours=4)


def test_a_bare_insert_is_not_the_same_write(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    accounts: 'sa.Table',
) -> None:
    """The trap `values_for_create` exists for, stated as a test.

    Nothing about the DDL is wrong here, so the Alembic gate in Phase 3 is
    silent about it; the INSERT is simply missing values Prisma would have sent.
    """
    with write_engine.connect() as conn:
        # SQLAlchemy warns first — the metadata says the primary key has no
        # default, because Prisma's is generated in the client. That warning is
        # the earliest signal available that a write is missing this module.
        with pytest.warns(sqlalchemy.exc.SAWarning, match='primary key'):
            with pytest.raises(sqlalchemy.exc.IntegrityError):
                conn.execute(
                    sqlalchemy.insert(accounts).values(
                        email='bare@example.com', url_slug='bare', balance=decimal.Decimal('1.50')
                    )
                )
        conn.rollback()

    assert read(write_engine, accounts, accounts.c.email == 'bare@example.com') is None


def test_a_duplicate_unique_is_refused_by_both_and_writes_nothing(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    accounts: 'sa.Table',
) -> None:
    """Same SQLSTATE, different exception class — which is the migration hazard.

    A caller that catches `prisma.errors.UniqueViolationError` catches nothing
    once the write is SQLAlchemy's; the equivalent is `IntegrityError`, and the
    only portable identification is the SQLSTATE.
    """
    # the generated client bundles its own runtime, so its error classes are
    # not `prisma.errors`' — reach for the ones the client actually raises
    errors = importlib.import_module(f'{CLIENT_PACKAGE}.errors')

    prisma_client.account.create(data=account_data('dup@example.com', 'dup'))

    with pytest.raises(errors.UniqueViolationError):
        prisma_client.account.create(data=account_data('dup@example.com', 'other'))

    with write_engine.connect() as conn:
        with pytest.raises(sqlalchemy.exc.IntegrityError) as caught:
            conn.execute(
                sqlalchemy.insert(accounts).values(
                    **values_for_create('Account', account_data('dup@example.com', 'another'))
                )
            )
        conn.rollback()

    assert getattr(caught.value.orig, 'sqlstate', None) == '23505'
    with write_engine.connect() as conn:
        assert (
            conn.execute(
                sqlalchemy.select(sqlalchemy.func.count())
                .select_from(accounts)
                .where(accounts.c.email == 'dup@example.com')
            ).scalar_one()
            == 1
        )


# -- update -------------------------------------------------------------------


def test_update_by_primary_key_lands_the_row_prisma_lands(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    accounts: 'sa.Table',
) -> None:
    """`db.account.update(where={'id': x}, data=...)` -> `update(t).where(t.c.id == x)`."""
    theirs = prisma_client.account.create(data=account_data('theirs@example.com', 'theirs'))
    ours = prisma_client.account.create(data=account_data('ours@example.com', 'ours'))

    prisma_client.account.update(where={'id': theirs.id}, data={'balance': decimal.Decimal('9.25')})

    with write_engine.connect() as conn:
        conn.execute(
            sqlalchemy.update(accounts)
            .where(accounts.c.id == ours.id)
            .values(**values_for_update('Account', {'balance': decimal.Decimal('9.25')}))
        )
        conn.commit()

    left = read(write_engine, accounts, accounts.c.id == theirs.id)
    right = read(write_engine, accounts, accounts.c.id == ours.id)
    assert left is not None and right is not None
    assert left['balance'] == right['balance'] == decimal.Decimal('9.25')
    assert fingerprint(left) == fingerprint(right)


def test_update_moves_updated_at_and_leaves_created_at_alone(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    accounts: 'sa.Table',
) -> None:
    row = prisma_client.account.create(data=account_data('ours@example.com', 'ours'))

    with write_engine.connect() as conn:
        conn.execute(
            sqlalchemy.update(accounts)
            .where(accounts.c.id == row.id)
            .values(**values_for_update('Account', {'slug': 'moved'}))
        )
        conn.commit()

    after = read(write_engine, accounts, accounts.c.id == row.id)
    assert after is not None
    assert after['updatedAt'] > row.updatedAt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    assert after['createdAt'] == row.createdAt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    assert after['url_slug'] == 'moved'


def test_update_by_compound_unique_reads_its_members_from_the_schema(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    accounts: 'sa.Table',
) -> None:
    """`where={'slug_role': {...}}`, and why the name cannot be split on `_`.

    `slug_role` is a *Prisma* identifier over *field* names. Its columns are
    `url_slug` and `role`, because `slug` is `@map`ped — so the reconstruction
    that splits the constraint name produces a column that does not exist, and
    that is before considering a field with an underscore in it.
    """
    columns = next(u for u in model_schema('Account')['uniques'] if u['name'] == 'slug_role')['columns']
    assert columns == ['url_slug', 'role']
    assert 'slug' not in accounts.c, 'splitting `slug_role` on `_` would name a column that is not there'

    theirs = prisma_client.account.create(data=account_data('theirs@example.com', 'shared', role='ADMIN'))
    ours = prisma_client.account.create(data=account_data('ours@example.com', 'shared', role='OWNER'))

    prisma_client.account.update(
        where={'slug_role': {'slug': 'shared', 'role': 'ADMIN'}},
        data={'balance': decimal.Decimal('4.00')},
    )

    with write_engine.connect() as conn:
        conn.execute(
            sqlalchemy.update(accounts)
            .where(
                sqlalchemy.and_(*(accounts.c[column] == value for column, value in zip(columns, ('shared', 'OWNER'))))
            )
            .values(**values_for_update('Account', {'balance': decimal.Decimal('4.00')}))
        )
        conn.commit()

    left = read(write_engine, accounts, accounts.c.id == theirs.id)
    right = read(write_engine, accounts, accounts.c.id == ours.id)
    assert left is not None and right is not None
    assert left['balance'] == right['balance'] == decimal.Decimal('4.00')


def test_update_by_compound_primary_key(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    write_metadata: 'sa.MetaData',
    installed_write_schema: None,
) -> None:
    """A compound `@@id` is in `primary_key`, and **not** in `uniques`.

    Prisma addresses it exactly like a compound unique — `where={'left_right':
    {...}}` — so an agent that only looks at `uniques` finds nothing and has no
    way to build the `where`.
    """
    spec = model_schema('Composite')
    assert spec['uniques'] == [], 'a compound @@id is not reported as a unique'
    assert spec['primary_key']['name'] == 'left_right'
    columns = spec['primary_key']['columns']

    composite = write_metadata.tables['Composite']
    prisma_client.composite.create(data={'left': 'l', 'right': 'r', 'note': 'theirs'})
    prisma_client.composite.create(data={'left': 'l2', 'right': 'r2', 'note': 'ours'})

    prisma_client.composite.update(where={'left_right': {'left': 'l', 'right': 'r'}}, data={'note': 'moved'})

    with write_engine.connect() as conn:
        conn.execute(
            sqlalchemy.update(composite)
            .where(sqlalchemy.and_(*(composite.c[column] == value for column, value in zip(columns, ('l2', 'r2')))))
            .values(**values_for_update('Composite', {'note': 'moved'}))
        )
        conn.commit()

    with write_engine.connect() as conn:
        notes = conn.execute(sqlalchemy.select(composite.c.note).order_by(composite.c.left)).scalars().all()
    assert notes == ['moved', 'moved']


def test_update_returning_is_the_row_that_was_written(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    accounts: 'sa.Table',
) -> None:
    """`update` returns the *post-update* row, and so does `RETURNING`."""
    row = prisma_client.account.create(data=account_data('ours@example.com', 'ours'))

    with write_engine.connect() as conn:
        returned = (
            conn.execute(
                sqlalchemy.update(accounts)
                .where(accounts.c.id == row.id)
                .values(**values_for_update('Account', {'slug': 'moved'}))
                .returning(*accounts.c)
            )
            .mappings()
            .one()
        )
        conn.commit()

    assert returned['url_slug'] == 'moved'
    assert dict(returned) == read(write_engine, accounts, accounts.c.id == row.id)


def test_update_with_no_matching_row_returns_none_rather_than_raising(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    accounts: 'sa.Table',
) -> None:
    """The behaviour that makes a naive translation wrong.

    `prisma-client-py` catches `RecordNotFoundError` and returns `None`, so a
    call site may well be relying on the miss. `.returning(...).first()`
    reproduces it; `.one()` would raise and change the program's control flow.
    """
    assert prisma_client.account.update(where={'id': 'absent'}, data={'slug': 'moved'}) is None

    with write_engine.connect() as conn:
        result = conn.execute(
            sqlalchemy.update(accounts)
            .where(accounts.c.id == 'absent')
            .values(**values_for_update('Account', {'slug': 'moved'}))
            .returning(*accounts.c)
        )
        assert result.mappings().first() is None
        conn.commit()


def test_update_with_no_matching_row_writes_nothing(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    accounts: 'sa.Table',
) -> None:
    """`@updatedAt` is generated before the `where` is evaluated, so this matters."""
    row = prisma_client.account.create(data=account_data('ours@example.com', 'ours'))

    with write_engine.connect() as conn:
        result = conn.execute(
            sqlalchemy.update(accounts)
            .where(accounts.c.id == 'absent')
            .values(**values_for_update('Account', {'slug': 'moved'}))
        )
        conn.commit()
        assert result.rowcount == 0

    after = read(write_engine, accounts, accounts.c.id == row.id)
    assert after is not None
    assert after['updatedAt'] == row.updatedAt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    assert after['url_slug'] == 'ours'


# -- delete -------------------------------------------------------------------


def test_delete_by_primary_key_removes_the_row_prisma_removes(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    accounts: 'sa.Table',
) -> None:
    theirs = prisma_client.account.create(data=account_data('theirs@example.com', 'theirs'))
    ours = prisma_client.account.create(data=account_data('ours@example.com', 'ours'))

    prisma_client.account.delete(where={'id': theirs.id})

    with write_engine.connect() as conn:
        conn.execute(sqlalchemy.delete(accounts).where(accounts.c.id == ours.id))
        conn.commit()

    assert read(write_engine, accounts, accounts.c.id == theirs.id) is None
    assert read(write_engine, accounts, accounts.c.id == ours.id) is None


def test_delete_by_compound_unique(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    accounts: 'sa.Table',
) -> None:
    columns = next(u for u in model_schema('Account')['uniques'] if u['name'] == 'slug_role')['columns']

    prisma_client.account.create(data=account_data('theirs@example.com', 'shared', role='ADMIN'))
    prisma_client.account.create(data=account_data('ours@example.com', 'shared', role='OWNER'))

    prisma_client.account.delete(where={'slug_role': {'slug': 'shared', 'role': 'ADMIN'}})

    with write_engine.connect() as conn:
        conn.execute(
            sqlalchemy.delete(accounts).where(
                sqlalchemy.and_(*(accounts.c[column] == value for column, value in zip(columns, ('shared', 'OWNER'))))
            )
        )
        conn.commit()

    with write_engine.connect() as conn:
        assert conn.execute(sqlalchemy.select(sqlalchemy.func.count()).select_from(accounts)).scalar_one() == 0


def test_delete_by_compound_primary_key(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    write_metadata: 'sa.MetaData',
    installed_write_schema: None,
) -> None:
    columns = model_schema('Composite')['primary_key']['columns']
    composite = write_metadata.tables['Composite']

    prisma_client.composite.create(data={'left': 'l', 'right': 'r'})
    prisma_client.composite.create(data={'left': 'l2', 'right': 'r2'})

    prisma_client.composite.delete(where={'left_right': {'left': 'l', 'right': 'r'}})

    with write_engine.connect() as conn:
        conn.execute(
            sqlalchemy.delete(composite).where(
                sqlalchemy.and_(*(composite.c[column] == value for column, value in zip(columns, ('l2', 'r2'))))
            )
        )
        conn.commit()

    with write_engine.connect() as conn:
        assert conn.execute(sqlalchemy.select(sqlalchemy.func.count()).select_from(composite)).scalar_one() == 0


def test_delete_returning_is_the_row_that_was_removed(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    accounts: 'sa.Table',
) -> None:
    """`delete` returns the deleted row; `RETURNING` is how you keep it."""
    row = prisma_client.account.create(data=account_data('ours@example.com', 'ours'))
    before = read(write_engine, accounts, accounts.c.id == row.id)

    with write_engine.connect() as conn:
        returned = (
            conn.execute(sqlalchemy.delete(accounts).where(accounts.c.id == row.id).returning(*accounts.c))
            .mappings()
            .one()
        )
        conn.commit()

    assert dict(returned) == before
    assert read(write_engine, accounts, accounts.c.id == row.id) is None


def test_delete_with_no_matching_row_returns_none_rather_than_raising(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    accounts: 'sa.Table',
) -> None:
    assert prisma_client.account.delete(where={'id': 'absent'}) is None

    with write_engine.connect() as conn:
        result = conn.execute(sqlalchemy.delete(accounts).where(accounts.c.id == 'absent').returning(*accounts.c))
        assert result.mappings().first() is None
        conn.commit()


def test_delete_leaves_the_cascade_to_the_foreign_key(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    write_metadata: 'sa.MetaData',
    installed_write_schema: None,
) -> None:
    """Nothing here names a relation — the `ON DELETE` in the DDL does the work.

    Worth pinning because the natural assumption is the opposite: that Prisma
    removes the children itself and a hand-written `DELETE` will hit a foreign
    key violation. It does not, and it will not.
    """
    accounts = write_metadata.tables['accounts']
    entries = write_metadata.tables['Entry']

    theirs = prisma_client.account.create(data=account_data('theirs@example.com', 'theirs'))
    ours = prisma_client.account.create(data=account_data('ours@example.com', 'ours'))
    for owner in (theirs, ours):
        prisma_client.entry.create(data={'accountId': owner.id, 'title': 'child'})

    prisma_client.account.delete(where={'id': theirs.id})

    with write_engine.connect() as conn:
        conn.execute(sqlalchemy.delete(accounts).where(accounts.c.id == ours.id))
        conn.commit()
        remaining = conn.execute(sqlalchemy.select(sqlalchemy.func.count()).select_from(entries)).scalar_one()

    assert remaining == 0, 'both cascades were performed by the database'


# -- what stays on the STOP list ----------------------------------------------


def test_a_nested_write_is_refused_rather_than_half_translated(installed_write_schema: None) -> None:
    """The boundary of this module, asserted so it cannot drift quietly."""
    with pytest.raises(LookupError, match='is a relation'):
        values_for_create('Account', {'email': 'a@example.com', 'posts': {'create': [{'title': 'x'}]}})


def test_an_atomic_update_is_refused_rather_than_written_as_a_mapping(installed_write_schema: None) -> None:
    with pytest.raises(NotImplementedError, match='atomic operation'):
        values_for_update('Account', {'visits': {'increment': 1}})
