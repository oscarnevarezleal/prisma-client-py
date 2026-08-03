"""Check each Prisma -> SQLAlchemy translation for a **transaction**.

`verify_writes.py` and `verify_bulk_writes.py` check what a statement writes.
This one checks the *boundary* around statements: when the work becomes visible
to anybody else, what happens to it when the block raises, and what the two
sides do when they are asked to nest.

Same two-pass shape as the bulk harness. Each case runs twice from a clean
slate — teardown, setup, the Prisma call, observe, teardown; then the same for
the SQLAlchemy call — and passes when the two passes agree on both what the call
returned and the rows it left behind. Every case carries an explicit
`'match'`/`'mismatch'` expectation, so a case that has gone vacuous fails loudly
rather than silently protecting nothing.

    BENCH_DATABASE_URL=postgresql://... python verify_transactions.py --workdir /tmp/pglab-sa

Exit code is the number of mismatches.

Everything this harness writes is prefixed `vtx-` and is deleted on the way in
and on the way out, so it can share a database with the seed data the read
harness needs.

Three things are deliberately **not** attempted here, and are refusals in the
runbook rather than gaps:

* a single transaction spanning a Prisma client and a SQLAlchemy connection.
  `two_transactions_in_one_block` measures why: the client is a separate process
  on its own connection, the two transaction ids differ, and neither can see the
  other's uncommitted rows. There is nothing to translate — there is a rule,
  which is to migrate a `tx()` block whole.
* an isolation level other than the default. `prisma-client-py`'s `tx()` takes
  `max_wait` and `timeout` and nothing else, so there is no second level to
  compare against.
* `SERIALIZABLE` retry loops, for the same reason.
"""

from __future__ import annotations

import os
import re
import sys
import time
import argparse
import datetime
import warnings
import importlib
import threading
import contextlib
from typing import Any, Dict, List, Tuple, Callable, Iterator, Optional, Sequence

import sqlalchemy as sa

#: Prefix on every id and name this harness writes. Teardown is "delete
#: everything starting with this", which is what lets the harness share a
#: database with the read harness's seed rows.
NS = 'vtx'

#: How long the lock cases hold a row before letting go. Long enough that a
#: 200ms `timeout` would have fired several times over if it were a lock
#: timeout, short enough that the harness still finishes.
HOLD = 1.2

#: (name, setup, prisma call, sqlalchemy call, observation, expectation).
#: `mismatch` pins a translation that is *wrong* — if one of those ever starts
#: agreeing, the case has gone vacuous and stops protecting anything.
Case = Tuple[
    str,
    Callable[[], None],
    Callable[[], Any],
    Callable[[], Any],
    Callable[[], Any],
    str,
]


def sqlalchemy_url(url: str) -> str:
    """`postgres://` -> the driver actually installed here."""
    for prefix in ('postgres://', 'postgresql://'):
        if url.startswith(prefix):
            return 'postgresql+psycopg://' + url[len(prefix) :]
    return url


def normalize(value: Any) -> Any:
    """Compare on values, not on the container the two APIs happen to use."""
    if isinstance(value, sa.engine.Row):
        return tuple(normalize(item) for item in value)
    if isinstance(value, sa.RowMapping):
        return {key: normalize(item) for key, item in dict(value).items()}
    if isinstance(value, (list, tuple)):
        return [normalize(item) for item in value]
    return value


# ---------------------------------------------------------------------------
# outcomes
# ---------------------------------------------------------------------------
#
# The two sides never raise the same class — Prisma raises out of
# `prisma.errors`, SQLAlchemy out of `sqlalchemy.exc` — so no case compares
# exception types. What is compared is whether the call refused at all, and,
# where PostgreSQL is the one refusing, the SQLSTATE, which both sides carry.


#: `code: "25P02"` in a Prisma engine error; `sqlstate` on a psycopg error.
SQLSTATE = re.compile(r'\b(\d\d[0-9A-Z]{3})\b')


def sqlstate_of(exc: BaseException) -> Optional[str]:
    original = getattr(exc, 'orig', None)
    state = getattr(original, 'sqlstate', None)
    if isinstance(state, str):
        return state
    match = SQLSTATE.search(str(exc))
    return match.group(1) if match else None


@contextlib.contextmanager
def nesting_is_expected() -> Iterator[None]:
    """Silence the `already in a transaction` warning the nesting cases provoke.

    `tx()` inside `tx()` warns, deliberately, and the client's own suite pins
    the warning. Here it is the thing being measured, not a surprise.
    """
    with warnings.catch_warnings():
        warnings.filterwarnings('ignore', message='The current client is already in a transaction.*')
        yield


def refused(call: Callable[[], Any]) -> Any:
    """`('raised',)` rather than the exception class."""
    try:
        return ('returned', call())
    except Exception:  # noqa: BLE001 - the type is deliberately not compared
        return ('raised',)


def refused_with_sqlstate(call: Callable[[], Any]) -> Any:
    """`('raised', '25P02')` — the SQLSTATE *is* comparable, the class is not."""
    try:
        return ('returned', call())
    except Exception as exc:  # noqa: BLE001
        return ('raised', sqlstate_of(exc))


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def teardown(conn: sa.Connection) -> None:
    conn.execute(sa.text(f'DELETE FROM "Tag" WHERE name LIKE \'{NS}-%\''))


def seed_tag(conn: sa.Connection, name: str) -> None:
    conn.execute(
        sa.text('INSERT INTO "Tag"(id,name) VALUES (:id,:name)'),
        {'id': f'{NS}-seed-{name}', 'name': f'{NS}-{name}'},
    )


# ---------------------------------------------------------------------------
# observations
# ---------------------------------------------------------------------------


def tag_names(conn: sa.Connection) -> List[str]:
    return sorted(conn.execute(sa.text(f'SELECT name FROM "Tag" WHERE name LIKE \'{NS}-%\'')).scalars())


def tag_shape(conn: sa.Connection) -> Dict[str, Any]:
    """Names, plus how many *transactions* wrote them.

    `xmin` is the id of the transaction that inserted the row. One distinct
    value across a set of rows is the database's own statement that they were
    written by one transaction — which is the whole claim `tx()` and `batch_()`
    make, and it is not observable any other way from outside.
    """
    rows = conn.execute(sa.text(f'SELECT name, xmin::text AS xmin FROM "Tag" WHERE name LIKE \'{NS}-%\'')).mappings()
    materialized = rows.all()
    return {
        'names': sorted(row['name'] for row in materialized),
        'writing transactions': len({row['xmin'] for row in materialized}),
    }


def nothing_observable() -> None:
    """For cases whose whole content is the value the call returned."""
    return None


# ---------------------------------------------------------------------------
# cases
# ---------------------------------------------------------------------------


def build_cases(  # noqa: C901 - a flat table of cases, not a branchy function
    db: Any,
    conn: sa.Connection,
    engine: sa.Engine,
    md: sa.MetaData,
    lab_errors: Any,
) -> List[Case]:
    from prisma.sa import values_for_create, values_for_update

    tag = md.tables['Tag']

    def insert(c: sa.Connection, name: str) -> Any:
        return c.execute(sa.insert(tag).values(**values_for_create('Tag', {'name': f'{NS}-{name}'})))

    def count_here(c: sa.Connection) -> int:
        return c.execute(
            sa.select(sa.func.count()).select_from(tag).where(tag.c.name.like(f'{NS}-%'))
        ).scalar_one()

    def count_there() -> int:
        return db.tag.count(where={'name': {'startsWith': NS}})

    def isolation(c: sa.Connection) -> str:
        return str(c.execute(sa.text('SHOW transaction_isolation')).scalar_one())

    def timeouts(c: sa.Connection) -> List[str]:
        return [
            str(c.execute(sa.text(f"SELECT current_setting('{setting}')")).scalar_one())
            for setting in ('statement_timeout', 'idle_in_transaction_session_timeout', 'lock_timeout')
        ]

    # -- the shared shape of the two lock cases -----------------------------
    #
    # A second connection takes a row lock and holds it for `HOLD` seconds. The
    # call under test then tries to update that row. What is compared is not the
    # elapsed time — that is a wall clock and would be flaky — but the *bucket*:
    # did the caller sit through the whole lock, or give up before it cleared?

    def under_lock(work: Callable[[], Any]) -> Any:
        holder = engine.connect()
        holder.execute(sa.text(f'SELECT * FROM "Tag" WHERE name = \'{NS}-locked\' FOR UPDATE'))

        def release() -> None:
            time.sleep(HOLD)
            holder.rollback()
            holder.close()

        releaser = threading.Thread(target=release)
        releaser.start()
        started = time.monotonic()
        outcome = refused(work)[0]
        elapsed = time.monotonic() - started
        releaser.join()
        return ('sat through the lock' if elapsed >= HOLD * 0.75 else 'gave up first', outcome)

    def prisma_waits_for_the_lock() -> Any:
        def work() -> Any:
            with db.tx(timeout=datetime.timedelta(milliseconds=20000)) as tx:
                return tx.tag.update(where={'name': f'{NS}-locked'}, data={'name': f'{NS}-moved'})

        return under_lock(work)

    def alchemy_waits_for_the_lock() -> Any:
        def work() -> Any:
            with engine.begin() as c:
                return c.execute(
                    sa.update(tag)
                    .where(tag.c.name == f'{NS}-locked')
                    .values(**values_for_update('Tag', {'name': f'{NS}-moved'}))
                ).rowcount

        return under_lock(work)

    def prisma_short_timeout_under_lock() -> Any:
        def work() -> Any:
            with db.tx(timeout=datetime.timedelta(milliseconds=200)) as tx:
                return tx.tag.update(where={'name': f'{NS}-locked'}, data={'name': f'{NS}-moved'})

        return under_lock(work)

    def alchemy_lock_timeout() -> Any:
        def work() -> Any:
            with engine.begin() as c:
                c.execute(sa.text("SET LOCAL lock_timeout = '200ms'"))
                return c.execute(
                    sa.update(tag)
                    .where(tag.c.name == f'{NS}-locked')
                    .values(**values_for_update('Tag', {'name': f'{NS}-moved'}))
                ).rowcount

        return under_lock(work)

    # -- setups -------------------------------------------------------------

    def nothing() -> None:
        return None

    def one_existing_tag() -> None:
        seed_tag(conn, 'dup')

    def a_row_to_lock() -> None:
        seed_tag(conn, 'locked')

    # -- the calls ----------------------------------------------------------

    def prisma_commits() -> None:
        with db.tx() as tx:
            tx.tag.create(data={'name': f'{NS}-a'})
            tx.tag.create(data={'name': f'{NS}-b'})

    def alchemy_commits() -> None:
        with engine.begin() as c:
            insert(c, 'a')
            insert(c, 'b')

    def prisma_raises() -> Any:
        def block() -> None:
            with db.tx() as tx:
                tx.tag.create(data={'name': f'{NS}-a'})
                raise RuntimeError('boom')

        return refused(block)

    def alchemy_raises() -> Any:
        def block() -> None:
            with engine.begin() as c:
                insert(c, 'a')
                raise RuntimeError('boom')

        return refused(block)

    def prisma_explicit_rollback() -> None:
        manager = db.tx()
        tx = manager.start()
        tx.tag.create(data={'name': f'{NS}-a'})
        manager.rollback()

    def alchemy_explicit_rollback() -> None:
        with engine.connect() as c:
            transaction = c.begin()
            insert(c, 'a')
            transaction.rollback()

    def prisma_no_transaction() -> None:
        db.tag.create(data={'name': f'{NS}-a'})
        db.tag.create(data={'name': f'{NS}-b'})

    def alchemy_no_transaction() -> None:
        with engine.connect() as c:
            insert(c, 'a')
            c.commit()
            insert(c, 'b')
            c.commit()

    def prisma_visibility() -> Any:
        with db.tx() as tx:
            tx.tag.create(data={'name': f'{NS}-a'})
            return (tx.tag.count(where={'name': {'startsWith': NS}}), count_there())

    def alchemy_visibility() -> Any:
        with engine.begin() as c:
            insert(c, 'a')
            return (count_here(c), count_here(conn))

    def prisma_after_the_block() -> Any:
        with db.tx() as tx:
            pass
        return refused(lambda: tx.tag.count())

    def alchemy_after_the_block() -> Any:
        with engine.begin() as c:
            pass
        return refused(lambda: c.execute(sa.text('SELECT 1')))

    def prisma_poisoned() -> Any:
        with db.tx() as tx:
            try:
                tx.tag.create(data={'name': f'{NS}-dup'})
            except lab_errors.UniqueViolationError:
                pass
            return refused_with_sqlstate(lambda: tx.tag.create(data={'name': f'{NS}-after'}))

    def alchemy_poisoned() -> Any:
        with engine.connect() as c:
            try:
                insert(c, 'dup')
            except sa.exc.IntegrityError:
                pass
            return refused_with_sqlstate(lambda: insert(c, 'after'))

    def prisma_cannot_recover() -> Any:
        def block() -> None:
            with db.tx() as tx:
                try:
                    tx.tag.create(data={'name': f'{NS}-dup'})
                except lab_errors.UniqueViolationError:
                    pass
                tx.tag.create(data={'name': f'{NS}-after'})

        return refused(block)

    def alchemy_savepoint_recovers() -> Any:
        def block() -> None:
            with engine.begin() as c:
                try:
                    with c.begin_nested():
                        insert(c, 'dup')
                except sa.exc.IntegrityError:
                    pass
                insert(c, 'after')

        return refused(block)

    def prisma_nested() -> Any:
        with nesting_is_expected(), db.tx() as outer:
            outer.tag.create(data={'name': f'{NS}-outer'})
            with outer.tx() as inner:
                seen = inner.tag.count(where={'name': {'startsWith': NS}})
                inner.tag.create(data={'name': f'{NS}-inner'})
            return ('inner saw', seen, 'outer then saw', outer.tag.count(where={'name': {'startsWith': NS}}))

    def alchemy_second_connection() -> Any:
        with engine.begin() as outer:
            insert(outer, 'outer')
            with engine.begin() as inner:
                seen = count_here(inner)
                insert(inner, 'inner')
            return ('inner saw', seen, 'outer then saw', count_here(outer))

    def alchemy_savepoint_nesting() -> Any:
        with engine.begin() as c:
            insert(c, 'outer')
            with c.begin_nested():
                seen = count_here(c)
                insert(c, 'inner')
            return ('inner saw', seen, 'outer then saw', count_here(c))

    def prisma_inner_rolls_back() -> Any:
        with nesting_is_expected(), db.tx() as outer:
            outer.tag.create(data={'name': f'{NS}-outer'})
            try:
                with outer.tx() as inner:
                    inner.tag.create(data={'name': f'{NS}-inner'})
                    raise RuntimeError('boom')
            except RuntimeError:
                pass
            return outer.tag.count(where={'name': {'startsWith': NS}})

    def alchemy_inner_rolls_back() -> Any:
        with engine.begin() as outer:
            insert(outer, 'outer')
            try:
                with engine.begin() as inner:
                    insert(inner, 'inner')
                    raise RuntimeError('boom')
            except RuntimeError:
                pass
            return count_here(outer)

    def prisma_isolation() -> Any:
        with db.tx() as tx:
            return str(tx.query_raw('SHOW transaction_isolation')[0]['transaction_isolation'])

    def alchemy_isolation() -> Any:
        with engine.begin() as c:
            return isolation(c)

    def prisma_timeouts() -> Any:
        with db.tx(timeout=datetime.timedelta(milliseconds=5000)) as tx:
            row = tx.query_raw(
                "SELECT current_setting('statement_timeout') a, "
                "current_setting('idle_in_transaction_session_timeout') b, "
                "current_setting('lock_timeout') c"
            )[0]
            return [str(row['a']), str(row['b']), str(row['c'])]

    def alchemy_timeouts() -> Any:
        with engine.begin() as c:
            return timeouts(c)

    def prisma_pool_exhausted() -> Any:
        managers = []
        try:
            outcome = refused(
                lambda: [
                    managers.append(started)
                    for started in (
                        _start(db, managers) for _ in range(40)  # noqa: B007
                    )
                ]
            )
        finally:
            for manager in managers:
                try:
                    manager.rollback()
                except Exception:  # noqa: BLE001 - best-effort cleanup
                    pass
        return outcome[0]

    def alchemy_pool_exhausted() -> Any:
        small = sa.create_engine(engine.url, pool_size=2, max_overflow=0, pool_timeout=0.2)
        held: List[sa.Connection] = []
        try:
            outcome = refused(lambda: [held.append(_open(small)) for _ in range(40)])
        finally:
            for c in held:
                c.rollback()
                c.close()
            small.dispose()
        return outcome[0]

    def prisma_batch() -> None:
        with db.batch_() as batcher:
            batcher.tag.create(data={'name': f'{NS}-a'})
            batcher.tag.create(data={'name': f'{NS}-b'})

    def alchemy_batch() -> None:
        with engine.begin() as c:
            insert(c, 'a')
            insert(c, 'b')

    def prisma_batch_conflict() -> Any:
        def block() -> None:
            with db.batch_() as batcher:
                batcher.tag.create(data={'name': f'{NS}-new'})
                batcher.tag.create(data={'name': f'{NS}-dup'})

        return refused(block)

    def alchemy_batch_conflict() -> Any:
        def block() -> None:
            with engine.begin() as c:
                insert(c, 'new')
                insert(c, 'dup')

        return refused(block)

    def prisma_empty_batch() -> None:
        with db.batch_():
            pass

    def alchemy_empty_batch() -> None:
        with engine.begin():
            pass

    def prisma_mixed_batch() -> None:
        with db.batch_() as batcher:
            batcher.tag.create(data={'name': f'{NS}-m1'})
            batcher.tag.create_many(data=[{'name': f'{NS}-m2'}, {'name': f'{NS}-m3'}])
            batcher.tag.update_many(where={'name': f'{NS}-m1'}, data={'name': f'{NS}-m1x'})
            batcher.tag.delete_many(where={'name': f'{NS}-m3'})

    def alchemy_mixed_batch() -> None:
        moment = datetime.datetime.now(datetime.timezone.utc)
        with engine.begin() as c:
            c.execute(sa.insert(tag).values(**values_for_create('Tag', {'name': f'{NS}-m1'}, moment=moment)))
            c.execute(
                sa.insert(tag).values(
                    [values_for_create('Tag', {'name': f'{NS}-{n}'}, moment=moment) for n in ('m2', 'm3')]
                )
            )
            c.execute(
                sa.update(tag)
                .where(tag.c.name == f'{NS}-m1')
                .values(**values_for_update('Tag', {'name': f'{NS}-m1x'}))
            )
            c.execute(sa.delete(tag).where(tag.c.name == f'{NS}-m3'))

    def prisma_batch_in_tx() -> Any:
        with db.tx() as tx:
            with tx.batch_() as batcher:
                batcher.tag.create(data={'name': f'{NS}-a'})
                batcher.tag.create(data={'name': f'{NS}-b'})
            return (count_there(), tx.tag.count(where={'name': {'startsWith': NS}}))

    def alchemy_batch_in_tx() -> Any:
        with engine.begin() as c:
            insert(c, 'a')
            insert(c, 'b')
            return (count_here(conn), count_here(c))

    def prisma_batch_returns() -> Any:
        with db.batch_() as batcher:
            return [batcher.tag.create(data={'name': f'{NS}-a'})]

    def alchemy_batch_returns() -> Any:
        with engine.begin() as c:
            return [insert(c, 'a').rowcount]

    def prisma_half_migrated_block() -> Any:
        """Half a `tx()` block migrated: one Prisma write, one SQLAlchemy write.

        This is the shape a call site takes if it is migrated one statement at a
        time, and it is the shape the runbook forbids. Both halves run inside
        what looks like one block and neither is inside the other's transaction.
        """
        with db.tx() as tx:
            tx.tag.create(data={'name': f'{NS}-one'})
            with engine.connect() as c:
                insert(c, 'two')
                same = int(tx.query_raw('SELECT txid_current() AS t')[0]['t']) == c.execute(
                    sa.text('SELECT txid_current()')
                ).scalar_one()
                both = count_here(c) == 2 and tx.tag.count(where={'name': {'startsWith': NS}}) == 2
                c.rollback()
        return ('one transaction', same, 'both writes visible inside', both)

    def alchemy_whole_block() -> Any:
        """The same block migrated whole — which is the translation."""
        with engine.begin() as c:
            insert(c, 'one')
            first = c.execute(sa.text('SELECT txid_current()')).scalar_one()
            insert(c, 'two')
            same = first == c.execute(sa.text('SELECT txid_current()')).scalar_one()
            both = count_here(c) == 2
        return ('one transaction', same, 'both writes visible inside', both)

    cases: List[Case] = [
        # -- tx(): the boundary ------------------------------------------------
        (
            'tx() commits everything in the block on a clean exit',
            nothing,
            prisma_commits,
            alchemy_commits,
            lambda: tag_shape(conn),
            'match',
        ),
        (
            'tx() rolls the whole block back when it raises',
            nothing,
            prisma_raises,
            alchemy_raises,
            lambda: tag_names(conn),
            'match',
        ),
        (
            'an explicit rollback() discards the block',
            nothing,
            prisma_explicit_rollback,
            alchemy_explicit_rollback,
            lambda: tag_names(conn),
            'match',
        ),
        (
            'a tx() writes every row in one database transaction',
            nothing,
            prisma_commits,
            alchemy_commits,
            lambda: tag_shape(conn)['writing transactions'],
            'match',
        ),
        # The control for the case above: without a transaction the same two
        # writes are two transactions, so "one xmin" is a real observation
        # rather than something every pair of rows would satisfy.
        (
            'without a transaction the same two writes are two transactions',
            nothing,
            prisma_no_transaction,
            alchemy_no_transaction,
            lambda: tag_shape(conn)['writing transactions'],
            'match',
        ),
        (
            'nothing in an open tx() is visible to another connection',
            nothing,
            prisma_visibility,
            alchemy_visibility,
            lambda: tag_names(conn),
            'match',
        ),
        (
            'using the transaction after the block is refused',
            nothing,
            prisma_after_the_block,
            alchemy_after_the_block,
            nothing_observable,
            'match',
        ),
        # -- tx(): failure inside the block ------------------------------------
        (
            'a failed statement poisons the rest of the transaction (SQLSTATE 25P02)',
            one_existing_tag,
            prisma_poisoned,
            alchemy_poisoned,
            lambda: tag_names(conn),
            'match',
        ),
        # The one thing SQLAlchemy can do that Prisma cannot. `begin_nested()`
        # is a SAVEPOINT, so the failing statement is undone and the block
        # carries on; `tx()` has no savepoint API at all, so catching the error
        # buys nothing and the whole block is still lost.
        (
            'a SAVEPOINT recovers from a caught error where tx() cannot',
            one_existing_tag,
            prisma_cannot_recover,
            alchemy_savepoint_recovers,
            lambda: tag_names(conn),
            'mismatch',
        ),
        # -- tx() inside tx() --------------------------------------------------
        (
            'tx() inside tx() is a second independent transaction',
            nothing,
            prisma_nested,
            alchemy_second_connection,
            lambda: tag_shape(conn),
            'match',
        ),
        # ... and specifically *not* a savepoint: a savepoint shares the outer
        # transaction, so the inner block can see the outer's uncommitted rows.
        # Prisma's inner transaction cannot.
        (
            'tx() inside tx() is not a SAVEPOINT',
            nothing,
            prisma_nested,
            alchemy_savepoint_nesting,
            lambda: tag_names(conn),
            'mismatch',
        ),
        (
            'an inner transaction rolling back leaves the outer one usable',
            nothing,
            prisma_inner_rolls_back,
            alchemy_inner_rolls_back,
            lambda: tag_names(conn),
            'match',
        ),
        # -- what the transaction is opened with -------------------------------
        (
            'both open at READ COMMITTED',
            nothing,
            prisma_isolation,
            alchemy_isolation,
            nothing_observable,
            'match',
        ),
        (
            'neither sets any PostgreSQL-side timeout',
            nothing,
            prisma_timeouts,
            alchemy_timeouts,
            nothing_observable,
            'match',
        ),
        # -- timeout / max_wait ------------------------------------------------
        (
            'both wait out a lock held by someone else',
            a_row_to_lock,
            prisma_waits_for_the_lock,
            alchemy_waits_for_the_lock,
            nothing_observable,
            'match',
        ),
        # `timeout` is not a lock timeout. With `timeout=200ms` against a lock
        # held for 1.2s, Prisma still sits through the whole wait and only then
        # discovers the transaction has expired. `SET LOCAL lock_timeout` gives
        # up at 200ms, which is a different thing entirely.
        (
            'timeout does not bound a lock wait, and lock_timeout is not its translation',
            a_row_to_lock,
            prisma_short_timeout_under_lock,
            alchemy_lock_timeout,
            nothing_observable,
            'mismatch',
        ),
        (
            'max_wait and pool_timeout both refuse rather than block forever',
            nothing,
            prisma_pool_exhausted,
            alchemy_pool_exhausted,
            nothing_observable,
            'match',
        ),
        # -- batch_() ----------------------------------------------------------
        (
            'batch_() writes every query in one transaction',
            nothing,
            prisma_batch,
            alchemy_batch,
            lambda: tag_shape(conn),
            'match',
        ),
        (
            'batch_() is all-or-nothing: one conflict discards the batch',
            one_existing_tag,
            prisma_batch_conflict,
            alchemy_batch_conflict,
            lambda: tag_names(conn),
            'match',
        ),
        (
            'an empty batch_() writes nothing',
            nothing,
            prisma_empty_batch,
            alchemy_empty_batch,
            lambda: tag_names(conn),
            'match',
        ),
        (
            'batch_() mixes create / create_many / update_many / delete_many',
            nothing,
            prisma_mixed_batch,
            alchemy_mixed_batch,
            lambda: tag_shape(conn),
            'match',
        ),
        (
            'batch_() inside tx() joins the outer transaction',
            nothing,
            prisma_batch_in_tx,
            alchemy_batch_in_tx,
            lambda: tag_shape(conn),
            'match',
        ),
        # A batch member returns `None`, so a call site inside a `batch_()`
        # already cannot read back what it wrote. The SQLAlchemy translation
        # returns a `CursorResult` — strictly more, and nothing to reproduce.
        (
            'a batch_() member returns nothing, a conn.execute() returns a result',
            nothing,
            prisma_batch_returns,
            alchemy_batch_returns,
            lambda: tag_names(conn),
            'mismatch',
        ),
        # -- the refusal, measured ---------------------------------------------
        # Half a `tx()` block migrated is two transactions that cannot see each
        # other; the same block migrated whole is one. This case is the reason
        # the runbook's rule is "migrate a tx() block whole or not at all", and
        # `mismatch` is what makes it a rule rather than a preference.
        (
            'half-migrating a tx() block gives two transactions, migrating it whole gives one',
            nothing,
            prisma_half_migrated_block,
            alchemy_whole_block,
            nothing_observable,
            'mismatch',
        ),
    ]
    return cases


def _start(db: Any, managers: List[Any]) -> Any:
    manager = db.tx(
        max_wait=datetime.timedelta(milliseconds=200),
        timeout=datetime.timedelta(milliseconds=20000),
    )
    manager.start()
    return manager


def _open(engine: sa.Engine) -> sa.Connection:
    conn = engine.connect()
    conn.begin()
    return conn


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------


def run_pass(
    conn: sa.Connection,
    setup: Callable[[], None],
    call: Callable[[], Any],
    observe: Callable[[], Any],
) -> Tuple[Any, Any]:
    teardown(conn)
    setup()
    returned = normalize(call())
    observed = normalize(observe())
    teardown(conn)
    return returned, observed


def report(results: Dict[str, str]) -> int:
    width = max(len(name) for name in results)
    failures = 0
    for name, outcome in results.items():
        status = outcome.split()[0]
        if status == 'FAIL' or status.endswith('ERROR'):
            failures += 1
        print(f'{status:9} {name:<{width}}  {outcome[len(status) :].strip()}')
    print(f'\n{len(results) - failures}/{len(results)} verified')
    return failures


def main(argv: Optional[Sequence[str]] = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--workdir', required=True)
    parser.add_argument('--package', default='pkg_sync')
    args = parser.parse_args(argv)

    url = os.environ['BENCH_DATABASE_URL']
    os.chdir(args.workdir)
    sys.path.insert(0, os.getcwd())

    mod = importlib.import_module(args.package)
    db = mod.Prisma(datasource={'url': url})
    db.connect()

    # The lab client is a *vendored copy* of the runtime, so its error classes
    # are not `prisma.errors`' classes — an `except prisma.errors.X` around a
    # lab-client call silently never fires. Catch out of the client that raised.
    lab_errors = importlib.import_module(f'{args.package}.errors')

    meta = importlib.import_module(f'{args.package}.metadata')

    import prisma.sa
    import prisma.metadata

    prisma.metadata.SCHEMA = meta.SCHEMA
    prisma.metadata.ENUM_SCHEMA = meta.ENUM_SCHEMA
    prisma.metadata.DATABASE_PROVIDER = 'postgresql'
    prisma.sa.clear_cache()

    from prisma.sa import build_metadata

    md = build_metadata(meta.SCHEMA, meta.ENUM_SCHEMA, 'postgresql')

    # Two engines, and the difference matters here more than anywhere else.
    # `observer` is AUTOCOMMIT: it is the third party every visibility case
    # asks "can you see this yet?", so it must never be inside a transaction of
    # its own. `engine` is the ordinary one the translations under test use.
    observer = sa.create_engine(sqlalchemy_url(url), isolation_level='AUTOCOMMIT')
    conn = observer.connect()
    engine = sa.create_engine(sqlalchemy_url(url))

    results: Dict[str, str] = {}
    for name, setup, prisma_call, alchemy_call, observe, expect in build_cases(db, conn, engine, md, lab_errors):
        try:
            left = run_pass(conn, setup, prisma_call, observe)
        except Exception as exc:  # noqa: BLE001
            teardown(conn)
            results[name] = f'PRISMA-ERROR {type(exc).__name__}: {exc}'
            continue
        try:
            right = run_pass(conn, setup, alchemy_call, observe)
        except Exception as exc:  # noqa: BLE001
            teardown(conn)
            results[name] = f'SA-ERROR {type(exc).__name__}: {exc}'
            continue

        matched = left == right
        if expect == 'match':
            results[name] = 'MATCH' if matched else f'FAIL differs\n    prisma={left!r}\n    sa    ={right!r}'
        else:
            results[name] = f'FAIL agreed, so the case proves nothing: {left!r}' if matched else 'DIFFERS-AS-EXPECTED'

    failures = report(results)

    conn.close()
    observer.dispose()
    engine.dispose()
    db.disconnect()
    sys.exit(failures)


if __name__ == '__main__':
    main()
