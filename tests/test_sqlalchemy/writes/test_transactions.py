"""What Prisma's transactions do, checked against Prisma.

`test_flat_writes.py` and `test_bulk_writes.py` pin what a statement writes.
This module pins the *boundary* around statements: when work becomes visible to
anybody else, what happens to it when the block raises, and what the two sides
do when they are asked to nest.

The translation is short — `db.tx()` is `engine.begin()`, and `db.batch_()` is
several statements inside one `engine.begin()` — and almost everything here
exists to pin the places where that is not the whole story:

* **A `tx()` inside a `tx()` is not a savepoint.** Prisma opens a second,
  independent transaction on a second connection, which cannot see the outer
  one's uncommitted rows. `conn.begin_nested()` *is* a savepoint and can. The
  translation of nested `tx()` is a second `engine.begin()`, not `begin_nested()`.
* **A failed statement poisons the rest of the transaction on both sides**, with
  the same SQLSTATE (`25P02`). Catching the error buys Prisma nothing, because
  it has no savepoint API; on the SQLAlchemy side `begin_nested()` recovers.
* **`timeout` is not a statement or lock timeout.** Neither side sets any
  PostgreSQL-side timeout, and Prisma sits through a lock wait far longer than
  its own `timeout` before noticing.

Everything asserted here is also a case in
`benchmarks/pg-lab/verify_transactions.py`, which runs the same comparisons
against the 41-model lab schema. This module is the copy that runs in CI.

One transaction, not two
------------------------

`connection` deliberately runs inside a transaction that is rolled back, and the
Prisma client — a separate process on its own connection — cannot join it. That
is not a quirk of the fixture, it is the same constraint a migrating application
is under, and it is why a `tx()` block must be migrated **whole**:
`test_half_migrating_a_tx_block_leaves_two_transactions` measures what happens
otherwise.
"""

from __future__ import annotations

import re
import time
import decimal
import datetime
import importlib
import threading
from typing import TYPE_CHECKING, Any, Dict, List, Tuple

import pytest

from prisma.sa import values_for_create

from .conftest import CLIENT_PACKAGE

if TYPE_CHECKING:
    import sqlalchemy as sa

sqlalchemy = pytest.importorskip('sqlalchemy', reason='prisma[sqlalchemy] is not installed')

#: `25P02` — `current transaction is aborted`. Prisma buries it in an engine
#: error's message, psycopg exposes it as an attribute; `sqlstate` takes it from
#: whichever place this side happens to keep it.
SQLSTATE = re.compile(r'\b(\d\d[0-9A-Z]{3})\b')

#: How long the lock cases hold a row before letting go. Long enough that a
#: 200ms `timeout` would have fired several times over if it were a lock
#: timeout, short enough that the suite still finishes.
HOLD = 1.2


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def account_data(slug: str) -> Dict[str, Any]:
    return {'email': f'{slug}@example.com', 'slug': slug, 'balance': decimal.Decimal('1.5')}


def insert(conn: 'sa.Connection', accounts: 'sa.Table', slug: str) -> Any:
    return conn.execute(sqlalchemy.insert(accounts).values(**values_for_create('Account', account_data(slug))))


def slugs_committed(engine: 'sa.Engine') -> List[str]:
    """What another connection can see — so, only what is committed."""
    with engine.connect() as conn:
        return sorted(conn.execute(sqlalchemy.text('SELECT url_slug FROM accounts')).scalars())


def writing_transactions(engine: 'sa.Engine') -> int:
    """How many database transactions wrote the rows currently in `accounts`.

    `xmin` is the id of the transaction that inserted a row. One distinct value
    across a set of rows is PostgreSQL's own statement that one transaction
    wrote them all, which is the claim `tx()` and `batch_()` both make and is
    not observable any other way from outside.
    """
    with engine.connect() as conn:
        return len(set(conn.execute(sqlalchemy.text('SELECT xmin::text FROM accounts')).scalars()))


def count_here(conn: 'sa.Connection') -> int:
    """Rows visible on *this* connection, committed or not."""
    return int(conn.execute(sqlalchemy.text('SELECT count(*) FROM accounts')).scalar_one())


def sqlstate(exc: BaseException) -> str:
    """The SQLSTATE, from wherever this side of the comparison keeps it."""
    state = getattr(getattr(exc, 'orig', None), 'sqlstate', None)
    if isinstance(state, str):
        return state
    found = SQLSTATE.search(str(exc))
    assert found is not None, f'no SQLSTATE in {exc}'
    return found.group(1)


def client_errors() -> Any:
    """The generated client's error classes.

    The client vendors the runtime, so `prisma.errors.UniqueViolationError` is a
    *different class* from the one it raises and an `except` on it never fires.
    """
    return importlib.import_module(f'{CLIENT_PACKAGE}.errors')


def under_lock(engine: 'sa.Engine', work: Any) -> Tuple[str, float]:
    """Run `work` while another connection holds a row lock for `HOLD` seconds.

    Returns whether the caller sat through the whole lock or gave up first. The
    elapsed time is returned too, but only ever asserted against `HOLD`, never
    against a fixed number — it is a wall clock.
    """
    holder = engine.connect()
    holder.execute(sqlalchemy.text("SELECT * FROM accounts WHERE url_slug = 'locked' FOR UPDATE"))

    def release() -> None:
        time.sleep(HOLD)
        holder.rollback()
        holder.close()

    releaser = threading.Thread(target=release)
    releaser.start()
    started = time.monotonic()
    try:
        work()
    except Exception:  # noqa: BLE001 - which side raised is not the claim
        pass
    elapsed = time.monotonic() - started
    releaser.join()
    return ('sat through the lock' if elapsed >= HOLD * 0.5 else 'gave up first', elapsed)


@pytest.fixture(name='accounts')
def accounts_fixture(write_metadata: 'sa.MetaData', installed_write_schema: None) -> 'sa.Table':
    return write_metadata.tables['accounts']


# ---------------------------------------------------------------------------
# tx(): the boundary
# ---------------------------------------------------------------------------


def test_tx_commits_the_whole_block_and_so_does_engine_begin(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    accounts: 'sa.Table',
) -> None:
    """`with db.tx() as tx:` -> `with engine.begin() as conn:`."""
    with prisma_client.tx() as tx:
        tx.account.create(data=account_data('a'))
        tx.account.create(data=account_data('b'))
        assert slugs_committed(write_engine) == [], 'nothing is committed until the block exits'

    theirs = slugs_committed(write_engine)
    assert theirs == ['a', 'b']

    prisma_client.account.delete_many()

    with write_engine.begin() as conn:
        insert(conn, accounts, 'a')
        insert(conn, accounts, 'b')
        assert slugs_committed(write_engine) == [], 'and the same is true here'

    assert slugs_committed(write_engine) == theirs


def test_tx_rolls_the_whole_block_back_when_it_raises(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    accounts: 'sa.Table',
) -> None:
    """Both context managers commit on a clean exit and roll back on any other.

    The exception is re-raised in both cases; neither swallows it.
    """
    with pytest.raises(RuntimeError, match='boom'):
        with prisma_client.tx() as tx:
            tx.account.create(data=account_data('a'))
            raise RuntimeError('boom')

    assert slugs_committed(write_engine) == []

    with pytest.raises(RuntimeError, match='boom'):
        with write_engine.begin() as conn:
            insert(conn, accounts, 'a')
            raise RuntimeError('boom')

    assert slugs_committed(write_engine) == []


def test_an_explicit_rollback_discards_the_block(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    accounts: 'sa.Table',
) -> None:
    """`db.tx()` used without `with` -> `conn.begin()` used without `with`.

    `TransactionManager.start()` / `.rollback()` is the un-nested form, and it
    maps onto the `Transaction` object `conn.begin()` hands back rather than onto
    `engine.begin()`, which has no way to be told not to commit.
    """
    manager = prisma_client.tx()
    tx = manager.start()
    tx.account.create(data=account_data('a'))
    manager.rollback()

    assert slugs_committed(write_engine) == []

    with write_engine.connect() as conn:
        transaction = conn.begin()
        insert(conn, accounts, 'a')
        transaction.rollback()

    assert slugs_committed(write_engine) == []


def test_a_transaction_writes_every_row_in_one_database_transaction(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    accounts: 'sa.Table',
) -> None:
    """One `xmin` across the batch — and two without the transaction.

    The second half is the control. Without it, "one distinct xmin" would be a
    property every pair of rows might happen to have, and the test would pass
    against a translation that never opened a transaction at all.
    """
    with prisma_client.tx() as tx:
        tx.account.create(data=account_data('a'))
        tx.account.create(data=account_data('b'))
    assert writing_transactions(write_engine) == 1

    prisma_client.account.delete_many()

    prisma_client.account.create(data=account_data('a'))
    prisma_client.account.create(data=account_data('b'))
    assert writing_transactions(write_engine) == 2, 'two calls, two transactions'

    prisma_client.account.delete_many()

    with write_engine.begin() as conn:
        insert(conn, accounts, 'a')
        insert(conn, accounts, 'b')
    assert writing_transactions(write_engine) == 1

    prisma_client.account.delete_many()

    with write_engine.connect() as conn:
        insert(conn, accounts, 'a')
        conn.commit()
        insert(conn, accounts, 'b')
        conn.commit()
    assert writing_transactions(write_engine) == 2


def test_using_the_transaction_after_the_block_is_refused(
    prisma_client: Any,
    write_engine: 'sa.Engine',
) -> None:
    """Both refuse, and the class is the only thing that differs.

    A call site that keeps the handle around gets `TransactionExpiredError` from
    Prisma and `ResourceClosedError` from SQLAlchemy — so a migration must move
    the `except` clause, but not the control flow.
    """
    errors = client_errors()

    with prisma_client.tx() as tx:
        pass

    with pytest.raises(errors.TransactionExpiredError):
        tx.account.count()

    with write_engine.begin() as conn:
        pass

    with pytest.raises(sqlalchemy.exc.ResourceClosedError):
        conn.execute(sqlalchemy.text('SELECT 1'))


# ---------------------------------------------------------------------------
# tx(): failure inside the block
# ---------------------------------------------------------------------------


def test_a_failed_statement_poisons_the_rest_of_the_transaction(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    accounts: 'sa.Table',
) -> None:
    """Catching the error does not make the transaction usable again.

    PostgreSQL aborts the transaction, and every following statement on it is
    refused with `25P02` until it ends — through Prisma exactly as through
    SQLAlchemy. A call site that catches a `UniqueViolationError` inside a
    `tx()` and carries on was already broken before the migration.
    """
    errors = client_errors()
    prisma_client.account.create(data=account_data('dup'))

    with prisma_client.tx() as tx:
        with pytest.raises(errors.UniqueViolationError):
            tx.account.create(data=account_data('dup'))

        with pytest.raises(errors.PrismaError) as theirs:
            tx.account.create(data=account_data('after'))

    assert sqlstate(theirs.value) == '25P02'
    assert slugs_committed(write_engine) == ['dup']

    with write_engine.connect() as conn:
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            insert(conn, accounts, 'dup')

        with pytest.raises(sqlalchemy.exc.DBAPIError) as ours:
            insert(conn, accounts, 'after')

    assert sqlstate(ours.value) == '25P02'
    assert slugs_committed(write_engine) == ['dup']


def test_a_savepoint_recovers_from_a_caught_error_where_tx_cannot(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    accounts: 'sa.Table',
) -> None:
    """The one thing SQLAlchemy can do here that Prisma cannot.

    `conn.begin_nested()` is a `SAVEPOINT`, so the failing statement is undone
    and the surrounding transaction survives. `tx()` exposes no savepoint at all,
    so the same shape loses the whole block. This is a capability the migration
    *gains* — not a translation, and not something to reach for while the two
    are still being compared.
    """
    prisma_client.account.create(data=account_data('dup'))

    with write_engine.begin() as conn:
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            with conn.begin_nested():
                insert(conn, accounts, 'dup')

        insert(conn, accounts, 'after')

    assert slugs_committed(write_engine) == ['after', 'dup'], 'the block carried on past the failure'


# ---------------------------------------------------------------------------
# tx() inside tx()
# ---------------------------------------------------------------------------


def test_tx_inside_tx_is_a_second_independent_transaction_not_a_savepoint(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    accounts: 'sa.Table',
) -> None:
    """What Prisma does with the inner one, measured rather than assumed.

    It warns — and then opens a wholly separate transaction on a separate
    connection, which cannot see the outer one's uncommitted rows and commits on
    its own. A second `engine.begin()` reproduces that. `conn.begin_nested()`
    does not: a savepoint shares the outer transaction, so the inner block sees
    the outer's row.
    """
    with pytest.warns(UserWarning, match='already in a transaction'):
        with prisma_client.tx() as outer:
            outer.account.create(data=account_data('outer'))
            with outer.tx() as inner:
                theirs = inner.account.count()
                inner.account.create(data=account_data('inner'))

    assert theirs == 0, 'the inner transaction cannot see the outer one'
    assert slugs_committed(write_engine) == ['inner', 'outer']
    assert writing_transactions(write_engine) == 2, 'two transactions, not one'

    prisma_client.account.delete_many()

    with write_engine.begin() as outer_conn:
        insert(outer_conn, accounts, 'outer')
        with write_engine.begin() as inner_conn:
            ours = count_here(inner_conn)
            insert(inner_conn, accounts, 'inner')

    assert ours == theirs
    assert slugs_committed(write_engine) == ['inner', 'outer']
    assert writing_transactions(write_engine) == 2

    prisma_client.account.delete_many()

    with write_engine.begin() as conn:
        insert(conn, accounts, 'outer')
        with conn.begin_nested():
            savepoint_saw = count_here(conn)
            insert(conn, accounts, 'inner')

    assert savepoint_saw == 1, 'a savepoint shares the outer transaction'
    assert savepoint_saw != theirs, 'which is why begin_nested() is not the translation'
    assert slugs_committed(write_engine) == ['inner', 'outer'], 'and commits with it'

    # `xmin` cannot tell the two apart. PostgreSQL gives each savepoint its own
    # *sub*transaction id, so rows written inside one carry a different `xmin`
    # from rows written before it even though one top-level transaction commits
    # them all. Visibility is the measurement that distinguishes them, and it is
    # the one asserted above.
    assert writing_transactions(write_engine) == 2


def test_an_inner_transaction_rolling_back_leaves_the_outer_one_usable(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    accounts: 'sa.Table',
) -> None:
    """The consequence of the inner one being independent.

    Because it is a different transaction rather than a savepoint, its rollback
    touches nothing the outer block did — and, unlike a failing statement on the
    outer transaction itself, does not poison it either.
    """
    with pytest.warns(UserWarning, match='already in a transaction'):
        with prisma_client.tx() as outer:
            outer.account.create(data=account_data('outer'))
            with pytest.raises(RuntimeError, match='boom'):
                with outer.tx() as inner:
                    inner.account.create(data=account_data('inner'))
                    raise RuntimeError('boom')
            assert outer.account.count() == 1

    theirs = slugs_committed(write_engine)
    assert theirs == ['outer']

    prisma_client.account.delete_many()

    with write_engine.begin() as outer_conn:
        insert(outer_conn, accounts, 'outer')
        with pytest.raises(RuntimeError, match='boom'):
            with write_engine.begin() as inner_conn:
                insert(inner_conn, accounts, 'inner')
                raise RuntimeError('boom')
        assert count_here(outer_conn) == 1

    assert slugs_committed(write_engine) == theirs


# ---------------------------------------------------------------------------
# what the transaction is opened with
# ---------------------------------------------------------------------------


def test_both_open_at_read_committed(
    prisma_client: Any,
    write_engine: 'sa.Engine',
) -> None:
    """The isolation level, measured from inside the transaction.

    `prisma-client-py`'s `tx()` takes `max_wait` and `timeout` and nothing else,
    so there is no isolation level to pass and no second level to diverge to.
    Both sides get PostgreSQL's `default_transaction_isolation`.
    """
    with prisma_client.tx() as tx:
        theirs = tx.query_raw('SHOW transaction_isolation')[0]['transaction_isolation']

    with write_engine.begin() as conn:
        ours = conn.execute(sqlalchemy.text('SHOW transaction_isolation')).scalar_one()

    assert ours == theirs == 'read committed'


def test_neither_side_sets_a_postgresql_timeout(
    prisma_client: Any,
    write_engine: 'sa.Engine',
) -> None:
    """`timeout` and `max_wait` leave no trace on the session.

    This is the mechanism behind `test_timeout_does_not_bound_a_lock_wait`: the
    engine enforces `timeout` with its own timer between queries, so a statement
    already blocked in the database is not covered by it.
    """
    settings = (
        "SELECT current_setting('statement_timeout') AS a, "
        "current_setting('idle_in_transaction_session_timeout') AS b, "
        "current_setting('lock_timeout') AS c"
    )

    with prisma_client.tx(timeout=datetime.timedelta(milliseconds=5000)) as tx:
        row = tx.query_raw(settings)[0]
        theirs = [row['a'], row['b'], row['c']]

    with write_engine.begin() as conn:
        ours = list(conn.execute(sqlalchemy.text(settings)).one())

    assert ours == theirs == ['0', '0', '0']


def test_timeout_does_not_bound_a_lock_wait(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    accounts: 'sa.Table',
) -> None:
    """`timeout` is not `lock_timeout`, and there is no translation for it.

    With `timeout=200ms` against a row locked for `HOLD` seconds, Prisma sits
    through the entire wait and only discovers the transaction has expired
    afterwards. `SET LOCAL lock_timeout` — the setting that *looks* like the
    translation — gives up while the lock is still held, which is a different
    behaviour, not the same one spelled differently.
    """
    prisma_client.account.create(data=account_data('locked'))

    def prisma_work() -> None:
        with prisma_client.tx(timeout=datetime.timedelta(milliseconds=200)) as tx:
            tx.account.update(where={'slug_role': {'slug': 'locked', 'role': 'VIEWER'}}, data={'balance': 2})

    theirs, waited = under_lock(write_engine, prisma_work)
    assert theirs == 'sat through the lock', f'gave up after {waited:.2f}s'

    def alchemy_work() -> None:
        with write_engine.begin() as conn:
            conn.execute(sqlalchemy.text("SET LOCAL lock_timeout = '200ms'"))
            conn.execute(
                sqlalchemy.update(accounts).where(accounts.c.url_slug == 'locked').values(balance=decimal.Decimal(2))
            )

    ours, _ = under_lock(write_engine, alchemy_work)
    assert ours == 'gave up first'
    assert ours != theirs, 'lock_timeout is not a translation of timeout'


def test_max_wait_and_pool_timeout_both_refuse_rather_than_block(
    prisma_client: Any,
    write_engine: 'sa.Engine',
) -> None:
    """The one part of `timeout`/`max_wait` that does have an analogue.

    `max_wait` bounds how long `tx()` waits for a free connection, and
    `pool_timeout` bounds exactly the same wait on the SQLAlchemy side. The
    *defaults* differ sharply — 2s against 30s — so it is an analogue to set
    deliberately, not one to inherit.
    """
    errors = client_errors()
    managers: List[Any] = []
    try:
        with pytest.raises(errors.PrismaError):
            # the bound is a backstop; the loop is meant to be cut short by the
            # pool refusing, and `pytest.raises` fails the test if it is not
            for _ in range(40):  # pragma: no branch
                manager = prisma_client.tx(
                    max_wait=datetime.timedelta(milliseconds=200),
                    timeout=datetime.timedelta(milliseconds=20000),
                )
                manager.start()
                managers.append(manager)
    finally:
        for manager in managers:
            manager.rollback()

    assert managers, 'the pool is finite, but it is not empty'

    small = sqlalchemy.create_engine(write_engine.url, pool_size=2, max_overflow=0, pool_timeout=0.2)
    held: List[Any] = []
    try:
        with pytest.raises(sqlalchemy.exc.TimeoutError):
            for _ in range(40):  # pragma: no branch
                conn = small.connect()
                conn.begin()
                held.append(conn)
    finally:
        for conn in held:
            conn.rollback()
            conn.close()
        small.dispose()

    assert len(held) == 2


# ---------------------------------------------------------------------------
# batch_()
# ---------------------------------------------------------------------------


def test_batch_writes_every_query_in_one_transaction(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    accounts: 'sa.Table',
) -> None:
    """`db.batch_()` is a transaction, not just a pipeline.

    The queries go to the engine in one payload, but what makes it translatable
    is that they land in one database transaction — which `engine.begin()`
    reproduces exactly.
    """
    with prisma_client.batch_() as batcher:
        batcher.account.create(data=account_data('a'))
        batcher.account.create(data=account_data('b'))

    theirs = slugs_committed(write_engine)
    assert theirs == ['a', 'b']
    assert writing_transactions(write_engine) == 1

    prisma_client.account.delete_many()

    with write_engine.begin() as conn:
        insert(conn, accounts, 'a')
        insert(conn, accounts, 'b')

    assert slugs_committed(write_engine) == theirs
    assert writing_transactions(write_engine) == 1


def test_a_batch_is_all_or_nothing(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    accounts: 'sa.Table',
) -> None:
    """One conflict discards the whole batch, including the queries before it."""
    errors = client_errors()
    prisma_client.account.create(data=account_data('dup'))

    with pytest.raises(errors.UniqueViolationError):
        with prisma_client.batch_() as batcher:
            batcher.account.create(data=account_data('new'))
            batcher.account.create(data=account_data('dup'))

    theirs = slugs_committed(write_engine)
    assert theirs == ['dup'], 'the good query before the conflict is gone too'

    prisma_client.account.delete_many()
    prisma_client.account.create(data=account_data('dup'))

    with pytest.raises(sqlalchemy.exc.IntegrityError):
        with write_engine.begin() as conn:
            insert(conn, accounts, 'new')
            insert(conn, accounts, 'dup')

    assert slugs_committed(write_engine) == theirs


def test_an_empty_batch_writes_nothing(
    prisma_client: Any,
    write_engine: 'sa.Engine',
) -> None:
    """`commit()` on an empty batch is not an error on either side."""
    with prisma_client.batch_():
        pass

    with write_engine.begin():
        pass

    assert slugs_committed(write_engine) == []


def test_a_batch_mixes_write_methods_inside_the_one_transaction(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    accounts: 'sa.Table',
) -> None:
    """A batch is not restricted to one model or one method.

    `create`, `create_many`, `update_many` and `delete_many` in one batch
    translate to the same four statements between one `engine.begin()`. The
    per-statement translations are §4.1's, unchanged.
    """
    with prisma_client.batch_() as batcher:
        batcher.account.create(data=account_data('one'))
        batcher.account.create_many(data=[account_data('two'), account_data('three')])
        batcher.account.update_many(where={'slug': 'one'}, data={'balance': 9})
        batcher.account.delete_many(where={'slug': 'three'})

    theirs = slugs_committed(write_engine)
    assert theirs == ['one', 'two']
    assert writing_transactions(write_engine) == 1

    prisma_client.account.delete_many()

    moment = datetime.datetime.now(datetime.timezone.utc)
    with write_engine.begin() as conn:
        conn.execute(
            sqlalchemy.insert(accounts).values(**values_for_create('Account', account_data('one'), moment=moment))
        )
        conn.execute(
            sqlalchemy.insert(accounts).values(
                [values_for_create('Account', account_data(slug), moment=moment) for slug in ('two', 'three')]
            )
        )
        conn.execute(sqlalchemy.update(accounts).where(accounts.c.url_slug == 'one').values(balance=decimal.Decimal(9)))
        conn.execute(sqlalchemy.delete(accounts).where(accounts.c.url_slug == 'three'))

    assert slugs_committed(write_engine) == theirs
    assert writing_transactions(write_engine) == 1


def test_a_batch_inside_a_tx_joins_the_outer_transaction(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    accounts: 'sa.Table',
) -> None:
    """`transaction.batch_()` does not open a transaction of its own.

    It runs inside the one already open, so nothing is committed until the outer
    block exits — which on the SQLAlchemy side is simply the same statements on
    the same connection, with no inner boundary to reproduce.
    """
    with prisma_client.tx() as tx:
        with tx.batch_() as batcher:
            batcher.account.create(data=account_data('a'))
            batcher.account.create(data=account_data('b'))

        assert slugs_committed(write_engine) == [], 'the batch committed nothing on its own'
        assert tx.account.count() == 2, 'but the outer transaction can see it'

    theirs = slugs_committed(write_engine)
    assert theirs == ['a', 'b']
    assert writing_transactions(write_engine) == 1

    prisma_client.account.delete_many()

    with write_engine.begin() as conn:
        insert(conn, accounts, 'a')
        insert(conn, accounts, 'b')
        assert slugs_committed(write_engine) == []
        assert count_here(conn) == 2

    assert slugs_committed(write_engine) == theirs
    assert writing_transactions(write_engine) == 1


def test_a_batch_member_returns_nothing(
    prisma_client: Any,
    write_engine: 'sa.Engine',
) -> None:
    """Which is why nothing has to be translated about a batch's results.

    `batcher.account.create(...)` returns `None` — a call site inside a
    `batch_()` already cannot read back what it wrote. `conn.execute()` returns a
    `CursorResult`, so the translation gives up nothing.
    """
    with prisma_client.batch_() as batcher:
        assert batcher.account.create(data=account_data('a')) is None
        assert batcher.account.delete_many(where={'slug': 'absent'}) is None

    assert slugs_committed(write_engine) == ['a']


# ---------------------------------------------------------------------------
# one transaction, not two
# ---------------------------------------------------------------------------


def test_half_migrating_a_tx_block_leaves_two_transactions(
    prisma_client: Any,
    write_engine: 'sa.Engine',
    accounts: 'sa.Table',
) -> None:
    """Why a `tx()` block is migrated whole or not at all.

    One statement translated and one left on Prisma looks like one block and is
    two transactions on two connections. Neither can see the other's rows, and
    an exception after the Prisma half committed does not undo it. Migrating the
    whole block gives one transaction and one rollback boundary.
    """
    with prisma_client.tx() as tx:
        tx.account.create(data=account_data('prisma'))
        with write_engine.connect() as conn:
            insert(conn, accounts, 'alchemy')
            assert count_here(conn) == 1, 'the SQLAlchemy half cannot see the Prisma half'
            assert tx.account.count() == 1, 'nor the other way round'
            conn.rollback()

    assert slugs_committed(write_engine) == ['prisma'], 'the rollback took only half the block'

    prisma_client.account.delete_many()

    with pytest.raises(RuntimeError, match='boom'):
        with write_engine.begin() as conn:
            insert(conn, accounts, 'prisma')
            insert(conn, accounts, 'alchemy')
            assert count_here(conn) == 2, 'one transaction, both rows'
            raise RuntimeError('boom')

    assert slugs_committed(write_engine) == [], 'and one rollback boundary'


def test_a_call_site_handed_a_connection_must_not_call_begin(
    connection: 'sa.Connection',
    accounts: 'sa.Table',
) -> None:
    """The other half of the same rule, and what the `connection` fixture is.

    A `Connection` autobegins on its first statement, so a helper that was
    `async with db.tx()` and becomes `conn.begin()` raises the moment its caller
    has already used the connection — which is exactly the situation this
    fixture, and any request-scoped transaction, puts it in. Take the
    `Connection` as a parameter and let the caller own the boundary;
    `begin_nested()` is the only inner boundary that composes, and it works on a
    fresh connection too.
    """
    insert(connection, accounts, 'a')
    assert connection.in_transaction(), 'autobegun by the statement above'

    with pytest.raises(sqlalchemy.exc.InvalidRequestError, match='already initialized'):
        connection.begin()

    savepoint = connection.begin_nested()
    assert connection.in_nested_transaction()
    insert(connection, accounts, 'b')
    savepoint.rollback()

    assert count_here(connection) == 1, 'the savepoint took only its own row'
