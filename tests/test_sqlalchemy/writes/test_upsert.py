"""`upsert`, checked against Prisma.

`upsert` is the one write that is not a single statement in Prisma either.
Measured against the query engine's own query log (CLI 5.19.0, PostgreSQL 16),
`db.<model>.upsert(where=W, data={'create': C, 'update': U})` compiles to

    INSERT ... VALUES (C) ON CONFLICT (<the columns of W's unique>)
        DO UPDATE SET U, "updatedAt" = $n WHERE W RETURNING *

**when `C` gives the fields named in `W` the same values `W` does**, and to a
`BEGIN; SELECT id WHERE W; UPDATE|INSERT; SELECT row; COMMIT` round trip when it
does not. That precondition is not cosmetic: `ON CONFLICT` keys on the row being
*inserted*, so once the two disagree the two forms address different rows —
`test_where_and_create_must_agree_on_the_key` is that divergence, executed.

Three consequences are what these tests exist to pin:

* the conflict target comes from the schema, and for a compound `@@id` it is
  **not** in `uniques` — that list is empty for such a model;
* the whole thing is one statement, so the insert is attempted even when the row
  exists (`test_the_insert_is_attempted_even_when_the_row_already_exists`) and a
  concurrent insert of the same key cannot break it, where a read-then-write
  translation raises;
* an empty `update` payload is a different operation, and stays on the STOP list.

The live harness for the same translation is `benchmarks/pg-lab/verify_upsert.py`,
which runs it against the 41-model lab schema. This module is the copy that runs
in CI.
"""

from __future__ import annotations

import decimal
import datetime
import importlib
from typing import Any, Dict, List, Mapping, Callable, Sequence

import pytest
import sqlalchemy as sa
from sqlalchemy import exc as sa_exc
from sqlalchemy.dialects import postgresql

from prisma.sa import values_for_create, values_for_update
from prisma._schema import model_schema

from .conftest import CLIENT_PACKAGE

RECENT = datetime.timedelta(minutes=5)

#: Columns two equivalent rows cannot agree on: the id is generated, and the
#: rest carry the values that keep the Prisma row and ours apart.
DISTINGUISHING = frozenset({'id', 'email', 'url_slug'})


# ---------------------------------------------------------------------------
# the translation under test
# ---------------------------------------------------------------------------


def conflict_target(model: str, where: Mapping[str, Any]) -> Sequence[str]:
    """The columns `ON CONFLICT` needs, read out of the schema rather than guessed.

    Prisma's `where` names a *Prisma* identifier and there are three places it
    can come from: a field name for a single `@id`/`@unique`, the constraint
    name for a compound `@@unique`, and the compound `@@id`'s name — which is in
    `primary_key` and **not** in `uniques`, so a lookup that only consults
    `uniques` finds nothing for exactly the models that need it most.
    """
    if len(where) != 1:
        raise LookupError(f'{model}: a `where` for upsert names exactly one unique, got {sorted(where)}')

    (key,) = where
    spec = model_schema(model)

    field = spec['fields'].get(key)
    if field is not None and (field['is_id'] or field['is_unique']):
        return [field['column']]

    for unique in spec['uniques']:
        if unique['name'] == key:
            return unique['columns']

    if spec['primary_key']['name'] == key:
        return spec['primary_key']['columns']

    raise LookupError(f'{model}.{key} is not a unique constraint, so it cannot be an ON CONFLICT target')


def upsert_statement(
    table: 'sa.Table',
    model: str,
    where: Mapping[str, Any],
    create: Mapping[str, Any],
    update: Mapping[str, Any],
) -> Any:
    """`db.<model>.upsert(where=..., data={'create': ..., 'update': ...})`.

    One `moment` for both halves, because the engine's statement carries one:
    the `updatedAt` bound into `DO UPDATE SET` is the same bind as the
    `createdAt` bound into `VALUES`.
    """
    moment = datetime.datetime.now(datetime.timezone.utc)
    return (
        postgresql.insert(table)
        .values(**values_for_create(model, create, moment=moment))
        .on_conflict_do_update(
            index_elements=list(conflict_target(model, where)),
            set_=values_for_update(model, update, moment=moment),
        )
        .returning(*table.c)
    )


def upsert(
    engine: 'sa.Engine',
    table: 'sa.Table',
    model: str,
    where: Mapping[str, Any],
    create: Mapping[str, Any],
    update: Mapping[str, Any],
) -> Dict[str, Any]:
    with engine.connect() as conn:
        row = conn.execute(upsert_statement(table, model, where, create, update)).mappings().one()
        conn.commit()
    return dict(row)


def read_then_write(
    engine: 'sa.Engine',
    table: 'sa.Table',
    model: str,
    predicate: Any,
    create: Mapping[str, Any],
    update: Mapping[str, Any],
    interleave: Callable[[], None],
) -> Dict[str, Any]:
    """The translation an agent writes when it has not heard of `ON CONFLICT`.

    It is here because the runbook argues against it, and the argument is only
    worth something if it was executed. `interleave` runs in the window between
    the read and the write — the window `ON CONFLICT` does not have.
    """
    with engine.connect() as conn:
        existing = conn.execute(sa.select(table).where(predicate)).mappings().first()
        interleave()
        moment = datetime.datetime.now(datetime.timezone.utc)
        if existing is None:
            statement: Any = postgresql.insert(table).values(**values_for_create(model, create, moment=moment))
        else:
            statement = sa.update(table).where(predicate).values(**values_for_update(model, update, moment=moment))
        row = conn.execute(statement.returning(*table.c)).mappings().one()
        conn.commit()
    return dict(row)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def account_data(email: str, slug: str, **extra: Any) -> Dict[str, Any]:
    return {'email': email, 'slug': slug, 'balance': decimal.Decimal('1.50'), **extra}


def stamp(value: datetime.datetime) -> Any:
    """A generated timestamp's comparable properties, since the value cannot be.

    Millisecond exactness is in here because Prisma's `DateTime` columns are
    `timestamp(3)`: a value that kept its microseconds is one the database
    rounded behind the caller's back.
    """
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    return (value.microsecond % 1000 == 0, abs(now - value) < RECENT)


def fingerprint(row: Mapping[str, Any]) -> Dict[str, Any]:
    """A stored row reduced to what two equivalent upserts must agree on."""
    out: Dict[str, Any] = {}
    for key, value in dict(row).items():
        if key in DISTINGUISHING:
            out[key] = f'<{key}>'
        elif isinstance(value, datetime.datetime):
            out[key] = stamp(value)
        else:
            out[key] = value
    return out


def read(engine: 'sa.Engine', table: 'sa.Table', where: Any) -> Dict[str, Any]:
    """The one row matching `where`.

    `.one()` rather than `.first()`: every call here reads back a row the test
    just wrote, so "there is no such row" is a failure and not a result.
    """
    with engine.connect() as conn:
        row = conn.execute(sa.select(table).where(where)).mappings().one()
    return dict(row)


def rows(engine: 'sa.Engine', table: 'sa.Table') -> List[Dict[str, Any]]:
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(sa.select(table)).mappings().all()]


def insert_elsewhere(engine: 'sa.Engine', table: 'sa.Table', model: str, data: Mapping[str, Any]) -> Callable[[], None]:
    """A second connection that writes the row and commits: a concurrent writer."""

    def write() -> None:
        with engine.connect() as other:
            other.execute(sa.insert(table).values(**values_for_create(model, data)))
            other.commit()

    return write


def nothing() -> None:
    """No concurrent writer — `read_then_write` with the window left empty."""


def naive(value: datetime.datetime) -> datetime.datetime:
    """Prisma's aware UTC datetime, as the `timestamp without time zone` it came from."""
    return value.astimezone(datetime.timezone.utc).replace(tzinfo=None)


@pytest.fixture(name='accounts')
def accounts_fixture(write_metadata: 'sa.MetaData', installed_write_schema: None) -> 'sa.Table':
    return write_metadata.tables['accounts']


# ---------------------------------------------------------------------------
# offline: where the conflict target comes from
# ---------------------------------------------------------------------------


def test_conflict_target_of_a_single_unique_field_is_its_column(installed: None) -> None:
    """Including `@map`: the where names the *field*, `ON CONFLICT` needs the column."""
    assert conflict_target('Account', {'email': 'a@example.com'}) == ['email']
    assert conflict_target('Account', {'id': 'x'}) == ['id']
    assert conflict_target('Profile', {'accountId': 'x'}) == ['account_id']


def test_conflict_target_of_a_compound_unique_comes_from_its_constraint_name(installed: None) -> None:
    """`slug_role` is a Prisma identifier over field names, not a column list.

    Splitting it on `_` yields `slug`, which is not a column — the field is
    `@map("url_slug")` — and that is before considering a field with an
    underscore in its own name.
    """
    assert conflict_target('Account', {'slug_role': {'slug': 'a', 'role': 'ADMIN'}}) == ['url_slug', 'role']


def test_conflict_target_of_a_compound_primary_key_is_not_in_uniques(installed: None) -> None:
    """The lookup that only reads `uniques` finds nothing here, and `uniques` is empty."""
    spec = model_schema('Composite')
    assert spec['uniques'] == []
    assert spec['primary_key']['name'] == 'left_right'
    assert conflict_target('Composite', {'left_right': {'left': 'l', 'right': 'r'}}) == ['left', 'right']


def test_conflict_target_refuses_a_field_that_is_not_unique(installed: None) -> None:
    """A non-unique `where` has no conflict target; Prisma refuses it too.

    Measured against the engine, `upsert(where={'title': ...})` comes back as
    `FieldNotFoundError: Could not find field at 'upsertOnePost.where'`.
    """
    with pytest.raises(LookupError, match='not a unique constraint'):
        conflict_target('Account', {'balance': decimal.Decimal(1)})
    with pytest.raises(LookupError, match='not a unique constraint'):
        conflict_target('Account', {'nonexistent': 1})


def test_conflict_target_refuses_a_where_naming_more_than_one_unique(installed: None) -> None:
    with pytest.raises(LookupError, match='exactly one unique'):
        conflict_target('Account', {'email': 'a@example.com', 'id': 'x'})


def test_one_moment_covers_both_branches_of_the_statement(installed: None) -> None:
    """The engine binds one instant into `VALUES` and into `DO UPDATE SET`.

    Calling `values_for_create` and `values_for_update` without sharing a moment
    reads the clock twice, which is invisible in a single-row test and is not
    what the statement being translated does.
    """
    moment = datetime.datetime(2023, 5, 4, 3, 2, 1, 123000, tzinfo=datetime.timezone.utc)
    create = values_for_create('Account', account_data('a@example.com', 'a'), moment=moment)
    update = values_for_update('Account', {'balance': decimal.Decimal(2)}, moment=moment)

    assert create['createdAt'] == create['updatedAt'] == update['updatedAt']
    assert 'createdAt' not in update, 'the update branch never touches createdAt'
    assert 'id' not in update, 'nor generates an id — the doomed one in VALUES is the only one'


def test_a_nested_write_in_either_payload_is_refused(installed: None, metadata: 'sa.MetaData') -> None:
    """Prisma performs nested writes inside `create`/`update`; this refuses them.

    That is a real divergence, and the reason nested writes stay on the STOP
    list: `values_for_*` names the field rather than half-translating it.
    """
    accounts = metadata.tables['accounts']
    with pytest.raises(LookupError, match='is a relation'):
        upsert_statement(
            accounts,
            'Account',
            {'email': 'a@example.com'},
            account_data('a@example.com', 'a', posts={'create': [{'title': 'x'}]}),
            {'balance': decimal.Decimal(2)},
        )
    with pytest.raises(NotImplementedError, match='atomic operation'):
        upsert_statement(
            accounts,
            'Account',
            {'email': 'a@example.com'},
            account_data('a@example.com', 'a'),
            {'visits': {'increment': 1}},
        )


# ---------------------------------------------------------------------------
# the create branch
# ---------------------------------------------------------------------------


def test_upsert_with_no_matching_row_lands_the_row_create_would_have(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    accounts: 'sa.Table',
) -> None:
    prisma_client.account.upsert(
        where={'email': 'theirs@example.com'},
        data={
            'create': account_data('theirs@example.com', 'theirs', role='ADMIN'),
            'update': {'balance': decimal.Decimal('9.99')},
        },
    )

    upsert(
        write_engine,
        accounts,
        'Account',
        {'email': 'ours@example.com'},
        account_data('ours@example.com', 'ours', role='ADMIN'),
        {'balance': decimal.Decimal('9.99')},
    )

    theirs = read(write_engine, accounts, accounts.c.email == 'theirs@example.com')
    ours = read(write_engine, accounts, accounts.c.email == 'ours@example.com')
    assert fingerprint(ours) == fingerprint(theirs)
    assert ours['balance'] == decimal.Decimal('1.50'), 'the update payload is not applied on this branch'
    assert ours['createdAt'] == ours['updatedAt'], 'as on a fresh create'
    assert ours['role'] == 'administrator', 'the create payload went through enum_label'


def test_upsert_create_branch_returns_the_row_it_wrote(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    accounts: 'sa.Table',
) -> None:
    """`upsert` returns the row on both branches, and so does `RETURNING`."""
    theirs = prisma_client.account.upsert(
        where={'email': 'theirs@example.com'},
        data={'create': account_data('theirs@example.com', 'theirs'), 'update': {'balance': decimal.Decimal(2)}},
    )
    returned = upsert(
        write_engine,
        accounts,
        'Account',
        {'email': 'ours@example.com'},
        account_data('ours@example.com', 'ours'),
        {'balance': decimal.Decimal(2)},
    )

    stored = read(write_engine, accounts, accounts.c.email == 'theirs@example.com')
    assert returned == read(write_engine, accounts, accounts.c.email == 'ours@example.com')
    assert fingerprint(returned) == fingerprint(stored)
    # what Prisma handed back is the same row it wrote, modulo the object types
    # `test_the_python_objects_prisma_returns_are_not_the_ones_returning_gives`
    # pins — so the two returns describe the same row as each other.
    assert theirs.id == stored['id']
    assert naive(theirs.createdAt) == stored['createdAt']
    assert theirs.balance == stored['balance'] == returned['balance']


# ---------------------------------------------------------------------------
# the update branch
# ---------------------------------------------------------------------------


def test_upsert_with_a_matching_row_updates_it_in_place(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    accounts: 'sa.Table',
) -> None:
    """The row keeps its id and its `createdAt`; `@updatedAt` moves."""
    theirs = prisma_client.account.create(data=account_data('theirs@example.com', 'theirs'))
    ours = prisma_client.account.create(data=account_data('ours@example.com', 'ours'))

    prisma_client.account.upsert(
        where={'email': 'theirs@example.com'},
        data={
            'create': account_data('theirs@example.com', 'theirs'),
            'update': {'balance': decimal.Decimal('9.99'), 'role': 'ADMIN'},
        },
    )
    upsert(
        write_engine,
        accounts,
        'Account',
        {'email': 'ours@example.com'},
        account_data('ours@example.com', 'ours'),
        {'balance': decimal.Decimal('9.99'), 'role': 'ADMIN'},
    )

    left = read(write_engine, accounts, accounts.c.email == 'theirs@example.com')
    right = read(write_engine, accounts, accounts.c.email == 'ours@example.com')
    assert fingerprint(left) == fingerprint(right)
    assert right['id'] == ours.id, 'the same row, not a replacement'
    assert right['createdAt'] == naive(ours.createdAt)
    assert right['updatedAt'] > naive(ours.updatedAt)
    assert right['balance'] == decimal.Decimal('9.99')
    assert right['role'] == 'administrator'
    assert left['id'] == theirs.id and left['createdAt'] == naive(theirs.createdAt)
    assert len(rows(write_engine, accounts)) == 2, 'neither side inserted a second row'


def test_upsert_update_branch_ignores_the_create_payload(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    accounts: 'sa.Table',
) -> None:
    prisma_client.account.create(data=account_data('theirs@example.com', 'theirs', role='OWNER'))
    prisma_client.account.create(data=account_data('ours@example.com', 'ours', role='OWNER'))

    prisma_client.account.upsert(
        where={'email': 'theirs@example.com'},
        data={
            'create': account_data('theirs@example.com', 'theirs', role='ADMIN'),
            'update': {'balance': decimal.Decimal('3.00')},
        },
    )
    upsert(
        write_engine,
        accounts,
        'Account',
        {'email': 'ours@example.com'},
        account_data('ours@example.com', 'ours', role='ADMIN'),
        {'balance': decimal.Decimal('3.00')},
    )

    left = read(write_engine, accounts, accounts.c.email == 'theirs@example.com')
    right = read(write_engine, accounts, accounts.c.email == 'ours@example.com')
    assert left['role'] == right['role'] == 'OWNER', 'the create payload is not applied on this branch'
    assert fingerprint(left) == fingerprint(right)


def test_upsert_update_branch_returns_the_post_update_row(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    accounts: 'sa.Table',
) -> None:
    ours = prisma_client.account.create(data=account_data('ours@example.com', 'ours'))

    returned = upsert(
        write_engine,
        accounts,
        'Account',
        {'email': 'ours@example.com'},
        account_data('ours@example.com', 'ours'),
        {'balance': decimal.Decimal('7.25')},
    )

    assert returned['id'] == ours.id
    assert returned['balance'] == decimal.Decimal('7.25')
    assert returned == read(write_engine, accounts, accounts.c.id == ours.id)


def test_the_python_objects_prisma_returns_are_not_the_ones_returning_gives(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    accounts: 'sa.Table',
) -> None:
    """The `create`/`update` caveat holds for `upsert` too: same row, different objects."""
    theirs = prisma_client.account.upsert(
        where={'email': 'theirs@example.com'},
        data={
            'create': account_data('theirs@example.com', 'theirs', role='ADMIN'),
            'update': {'balance': decimal.Decimal(2)},
        },
    )
    returned = upsert(
        write_engine,
        accounts,
        'Account',
        {'email': 'ours@example.com'},
        account_data('ours@example.com', 'ours', role='ADMIN'),
        {'balance': decimal.Decimal(2)},
    )

    assert theirs.role == 'ADMIN', 'Prisma hands back the member name'
    assert returned['role'] == 'administrator', 'RETURNING hands back the stored label'
    assert theirs.createdAt.tzinfo is not None, 'Prisma hands back an aware UTC datetime'
    assert returned['createdAt'].tzinfo is None, '`timestamp without time zone` decodes to naive'


# ---------------------------------------------------------------------------
# the where: every shape of unique
# ---------------------------------------------------------------------------


def test_upsert_by_a_compound_unique(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    accounts: 'sa.Table',
) -> None:
    """Both branches, keyed on `@@unique([slug, role])`."""
    prisma_client.account.upsert(
        where={'slug_role': {'slug': 'theirs', 'role': 'ADMIN'}},
        data={
            'create': account_data('theirs@example.com', 'theirs', role='ADMIN'),
            'update': {'balance': decimal.Decimal('4.00')},
        },
    )
    prisma_client.account.upsert(
        where={'slug_role': {'slug': 'theirs', 'role': 'ADMIN'}},
        data={
            'create': account_data('theirs@example.com', 'theirs', role='ADMIN'),
            'update': {'balance': decimal.Decimal('4.00')},
        },
    )

    for _ in range(2):
        upsert(
            write_engine,
            accounts,
            'Account',
            {'slug_role': {'slug': 'ours', 'role': 'ADMIN'}},
            account_data('ours@example.com', 'ours', role='ADMIN'),
            {'balance': decimal.Decimal('4.00')},
        )

    left = read(write_engine, accounts, accounts.c.email == 'theirs@example.com')
    right = read(write_engine, accounts, accounts.c.email == 'ours@example.com')
    assert left['balance'] == right['balance'] == decimal.Decimal('4.00')
    assert fingerprint(left) == fingerprint(right)
    assert len(rows(write_engine, accounts)) == 2, 'two upserts each, two rows'


def test_upsert_by_a_compound_primary_key(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    write_metadata: 'sa.MetaData',
    installed_write_schema: None,
) -> None:
    """A compound `@@id`, whose columns are in `primary_key` and nowhere else."""
    composite = write_metadata.tables['Composite']

    for note in ('created', 'updated'):
        prisma_client.composite.upsert(
            where={'left_right': {'left': 'l', 'right': 'r'}},
            data={'create': {'left': 'l', 'right': 'r', 'note': 'created'}, 'update': {'note': note}},
        )
        upsert(
            write_engine,
            composite,
            'Composite',
            {'left_right': {'left': 'l2', 'right': 'r2'}},
            {'left': 'l2', 'right': 'r2', 'note': 'created'},
            {'note': note},
        )

    assert sorted((row['left'], row['note']) for row in rows(write_engine, composite)) == [
        ('l', 'updated'),
        ('l2', 'updated'),
    ]


# ---------------------------------------------------------------------------
# it is one statement
# ---------------------------------------------------------------------------


def test_the_insert_is_attempted_even_when_the_row_already_exists(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    write_metadata: 'sa.MetaData',
    installed_write_schema: None,
) -> None:
    """The measurement that separates one statement from a round trip.

    `Profile.id` is a `SERIAL`. `INSERT ... ON CONFLICT DO UPDATE` forms the
    candidate row before discovering the conflict, so the sequence advances even
    on the update branch — where a `SELECT` that found the row and issued an
    `UPDATE` would never have touched it. Prisma advances it by exactly as much
    as the translation does, which is how we know it is not round-tripping.
    """
    profiles = write_metadata.tables['Profile']
    theirs = prisma_client.account.create(data=account_data('theirs@example.com', 'theirs'))
    ours = prisma_client.account.create(data=account_data('ours@example.com', 'ours'))

    def sequence() -> int:
        with write_engine.connect() as conn:
            name = conn.execute(sa.text("SELECT pg_get_serial_sequence('\"Profile\"', 'id')")).scalar_one()
            return int(conn.execute(sa.text(f'SELECT last_value FROM {name}')).scalar_one())

    for _ in range(2):
        prisma_client.profile.upsert(
            where={'accountId': theirs.id},
            data={'create': {'accountId': theirs.id, 'bio': 'created'}, 'update': {'bio': 'updated'}},
        )
    theirs_advance = sequence()

    for _ in range(2):
        upsert(
            write_engine,
            profiles,
            'Profile',
            {'accountId': ours.id},
            {'accountId': ours.id, 'bio': 'created'},
            {'bio': 'updated'},
        )
    ours_advance = sequence() - theirs_advance

    assert theirs_advance == ours_advance == 2, 'two calls, two ids consumed, one row each'
    assert len(rows(write_engine, profiles)) == 2
    assert sorted(row['bio'] for row in rows(write_engine, profiles)) == ['updated', 'updated']


def test_upsert_survives_a_concurrent_insert_of_the_same_key(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    accounts: 'sa.Table',
) -> None:
    """Another connection lands the row first; both callers take the update branch."""
    insert_elsewhere(write_engine, accounts, 'Account', account_data('theirs@example.com', 'theirs'))()
    prisma_client.account.upsert(
        where={'email': 'theirs@example.com'},
        data={'create': account_data('theirs@example.com', 'theirs'), 'update': {'balance': decimal.Decimal('5.00')}},
    )

    insert_elsewhere(write_engine, accounts, 'Account', account_data('ours@example.com', 'ours'))()
    upsert(
        write_engine,
        accounts,
        'Account',
        {'email': 'ours@example.com'},
        account_data('ours@example.com', 'ours'),
        {'balance': decimal.Decimal('5.00')},
    )

    left = read(write_engine, accounts, accounts.c.email == 'theirs@example.com')
    right = read(write_engine, accounts, accounts.c.email == 'ours@example.com')
    assert left['balance'] == right['balance'] == decimal.Decimal('5.00')
    assert len(rows(write_engine, accounts)) == 2


def test_a_read_then_write_translation_does_not_survive_a_concurrent_insert(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    accounts: 'sa.Table',
) -> None:
    """The same losing race, executed against the translation the runbook rejects.

    Another writer lands the row; the difference is *where* it can land. Prisma's
    statement has no window, so the only place is before it — and it takes the
    update branch. The read-then-write form has a window between its `SELECT` and
    its `INSERT`, the other writer lands in it, and the `INSERT` is a unique
    violation instead.

    `1.50` is the create payload's balance and `5.00` only ever comes from the
    update payload, so which branch ran is readable off the row.
    """
    insert_elsewhere(write_engine, accounts, 'Account', account_data('theirs@example.com', 'theirs'))()
    prisma_client.account.upsert(
        where={'email': 'theirs@example.com'},
        data={'create': account_data('theirs@example.com', 'theirs'), 'update': {'balance': decimal.Decimal('5.00')}},
    )
    theirs = read(write_engine, accounts, accounts.c.email == 'theirs@example.com')
    assert theirs['balance'] == decimal.Decimal('5.00'), 'Prisma updated the row the other writer landed'

    with pytest.raises(sa_exc.IntegrityError) as caught:
        read_then_write(
            write_engine,
            accounts,
            'Account',
            accounts.c.email == 'ours@example.com',
            account_data('ours@example.com', 'ours'),
            {'balance': decimal.Decimal('5.00')},
            interleave=insert_elsewhere(write_engine, accounts, 'Account', account_data('ours@example.com', 'ours')),
        )

    assert getattr(caught.value.orig, 'sqlstate', None) == '23505'
    ours = read(write_engine, accounts, accounts.c.email == 'ours@example.com')
    assert ours['balance'] == decimal.Decimal(
        '1.50'
    ), 'the row the other writer landed, untouched: our write never happened'


def test_the_read_then_write_translation_is_right_only_when_nothing_races_it(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    accounts: 'sa.Table',
) -> None:
    """Both of its branches, with the window left empty — which is the point.

    It lands the same rows as `ON CONFLICT` does. What it does not have is
    atomicity, and nothing about the result of an uncontended run says so.
    """
    created = read_then_write(
        write_engine,
        accounts,
        'Account',
        accounts.c.email == 'ours@example.com',
        account_data('ours@example.com', 'ours'),
        {'balance': decimal.Decimal('5.00')},
        interleave=nothing,
    )
    updated = read_then_write(
        write_engine,
        accounts,
        'Account',
        accounts.c.email == 'ours@example.com',
        account_data('ours@example.com', 'ours'),
        {'balance': decimal.Decimal('5.00')},
        interleave=nothing,
    )

    prisma_client.account.upsert(
        where={'email': 'theirs@example.com'},
        data={'create': account_data('theirs@example.com', 'theirs'), 'update': {'balance': decimal.Decimal('5.00')}},
    )
    prisma_client.account.upsert(
        where={'email': 'theirs@example.com'},
        data={'create': account_data('theirs@example.com', 'theirs'), 'update': {'balance': decimal.Decimal('5.00')}},
    )

    theirs = read(write_engine, accounts, accounts.c.email == 'theirs@example.com')
    assert created['id'] == updated['id'], 'the second pass took the update branch'
    assert updated['balance'] == decimal.Decimal('5.00')
    assert fingerprint(updated) == fingerprint(theirs)


# ---------------------------------------------------------------------------
# what stays on the STOP list
# ---------------------------------------------------------------------------


def test_where_and_create_must_agree_on_the_key(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    accounts: 'sa.Table',
) -> None:
    """The precondition, executed — and the divergence when it does not hold.

    Prisma's `where` selects the row; `ON CONFLICT` keys on the row being
    inserted. When `create` gives the unique a different value the two address
    different rows, and Prisma stops emitting the single statement at all: it
    round-trips instead (observed in the engine's query log). The translation
    inserts a second row where Prisma updates the first.
    """
    prisma_client.account.create(data=account_data('theirs@example.com', 'theirs'))
    prisma_client.account.upsert(
        where={'email': 'theirs@example.com'},
        data={
            'create': account_data('other@example.com', 'other'),
            'update': {'balance': decimal.Decimal('8.00')},
        },
    )
    assert sorted(row['email'] for row in rows(write_engine, accounts)) == ['theirs@example.com']
    assert prisma_client.account.find_unique(where={'email': 'theirs@example.com'}).balance == decimal.Decimal('8.00')

    upsert(
        write_engine,
        accounts,
        'Account',
        {'email': 'theirs@example.com'},
        account_data('other@example.com', 'other'),
        {'balance': decimal.Decimal('8.00')},
    )
    assert sorted(row['email'] for row in rows(write_engine, accounts)) == [
        'other@example.com',
        'theirs@example.com',
    ], 'ON CONFLICT conflicted on nothing, so it inserted'


def test_a_conflict_on_a_different_unique_is_not_caught_by_the_conflict_target(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    accounts: 'sa.Table',
) -> None:
    """`ON CONFLICT (a)` says nothing about a violation of unique `b`.

    Both callers raise, and neither writes a row — but the exception class
    changes, so a call site catching `UniqueViolationError` catches nothing.
    """
    # the generated client bundles its own runtime, so its error classes are not
    # `prisma.errors`' — reach for the ones the client actually raises
    errors = importlib.import_module(f'{CLIENT_PACKAGE}.errors')

    prisma_client.account.create(data=account_data('taken@example.com', 'taken', role='ADMIN'))

    with pytest.raises(errors.UniqueViolationError):
        prisma_client.account.upsert(
            where={'slug_role': {'slug': 'free', 'role': 'ADMIN'}},
            data={
                'create': account_data('taken@example.com', 'free', role='ADMIN'),
                'update': {'balance': decimal.Decimal(2)},
            },
        )

    with pytest.raises(sa_exc.IntegrityError) as caught:
        upsert(
            write_engine,
            accounts,
            'Account',
            {'slug_role': {'slug': 'free', 'role': 'ADMIN'}},
            account_data('taken@example.com', 'free', role='ADMIN'),
            {'balance': decimal.Decimal(2)},
        )

    assert getattr(caught.value.orig, 'sqlstate', None) == '23505'
    assert len(rows(write_engine, accounts)) == 1


def test_an_empty_update_payload_is_a_different_operation(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    write_metadata: 'sa.MetaData',
    accounts: 'sa.Table',
) -> None:
    """`update={}` is not `DO UPDATE SET nothing`, and it is not a no-op either.

    Measured: Prisma drops out of the single statement and writes **nothing** —
    `@updatedAt` does not move. The translation cannot express that: on a model
    with `@updatedAt`, `values_for_update({})` is `{'updatedAt': ...}`, so the
    row would be stamped; on a model without one the `set_` is empty and
    SQLAlchemy refuses to compile the statement.
    """
    theirs = prisma_client.account.create(data=account_data('theirs@example.com', 'theirs'))
    prisma_client.account.upsert(
        where={'email': 'theirs@example.com'},
        data={'create': account_data('theirs@example.com', 'theirs'), 'update': {}},
    )

    after = read(write_engine, accounts, accounts.c.email == 'theirs@example.com')
    assert after['updatedAt'] == naive(theirs.updatedAt), 'Prisma left the row alone entirely'

    assert values_for_update('Account', {}) != {}, 'ours would stamp @updatedAt'
    with pytest.raises(ValueError, match='must not be empty'):
        upsert_statement(
            write_metadata.tables['Composite'],
            'Composite',
            {'left_right': {'left': 'l', 'right': 'r'}},
            {'left': 'l', 'right': 'r'},
            {},
        )
