"""Verified helpers for a Prisma → SQLAlchemy migration.

Copy this into the project being migrated. Do not retype it from memory: every
line is the result of a measurement against a live PostgreSQL database with both
clients, and the plausible-looking simplifications are the ones that are wrong.

What is deliberate here, and what breaks if you change it:

* one ``moment`` per batch/statement — Prisma stamps a whole batch with a single
  instant; a fresh clock reading per row is invisible in a single-row test
* one INSERT per distinct key set — ``sa.insert(t).values([...])`` compiles its
  ``VALUES`` clause from the *first* mapping and silently drops keys only later
  rows carry, with no error and the right row count
* counts from ``RETURNING`` — ``rowcount`` is ``-1`` for INSERT under psycopg
  (it *is* correct for UPDATE and DELETE)
* ``on_conflict_do_update`` rather than SELECT-then-write — a read-then-write
  translation lands the same rows on every uncontended run and raises 23505
  under a concurrent insert of the same key, so no test will tell you it is wrong

Everything here is SQLAlchemy Core against ``prisma.sa`` tables. PostgreSQL only.
"""

from __future__ import annotations

import datetime
import itertools
import importlib.util
from typing import Any, Dict, List, Mapping, Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from prisma import sa as prisma_sa
from prisma.sa import values_for_create, values_for_update
from prisma._schema import relation, enum_label, model_schema  # noqa: F401  (re-exported for call sites)

# ---------------------------------------------------------------------------
# engine
# ---------------------------------------------------------------------------


def create_engine(database_url: str, **kwargs: Any) -> sa.Engine:
    """An engine for the URL the application already has.

    SQLAlchemy defaults ``postgresql://`` to psycopg2. Do **not** rewrite the URL
    to ``postgresql+psycopg://`` unless psycopg 3 is actually installed — most
    applications ship ``psycopg2-binary`` and the rewrite gives
    ``ModuleNotFoundError: No module named 'psycopg'``.

    ``pool_timeout`` is the analogue of Prisma's ``tx(max_wait=...)``: both bound
    how long a caller waits for a *connection* and both refuse rather than block
    forever. The defaults are far apart — 2s for ``max_wait``, 30s for
    ``pool_timeout`` — so pass it deliberately if any call site set ``max_wait``.
    """
    if database_url.startswith('postgresql://') and importlib.util.find_spec('psycopg2') is None:
        database_url = database_url.replace('postgresql://', 'postgresql+psycopg://', 1)
    return sa.create_engine(database_url, **kwargs)


# ---------------------------------------------------------------------------
# create_many
# ---------------------------------------------------------------------------


def insert_many(
    conn: sa.Connection,
    table: sa.Table,
    model: str,
    data: Sequence[Mapping[str, Any]],
    *,
    skip_duplicates: bool = False,
) -> int:
    """``db.<model>.create_many(data=data, skip_duplicates=...)``.

    Returns the number of rows inserted, as Prisma does.

    ``skip_duplicates`` matches Prisma both against an already-present row and
    against a duplicate appearing twice *inside* the batch — PostgreSQL resolves
    the latter within the one statement, so no de-duplication is needed first.
    Without it a conflict aborts the **whole** batch and inserts nothing;
    ``prisma.errors.UniqueViolationError`` becomes ``sqlalchemy.exc.IntegrityError``
    (identify it by ``exc.orig.sqlstate == '23505'``).

    Prisma's ``create_many`` is one unit and this is one statement *per key set*,
    so call it inside ``engine.begin()`` or an already-open transaction — on
    autocommit a failure in the second group would leave the first committed.
    """
    moment = datetime.datetime.now(datetime.timezone.utc)
    rows = [values_for_create(model, item, moment=moment) for item in data]

    inserted = 0
    for _, group in itertools.groupby(
        sorted(rows, key=lambda row: sorted(row)),
        key=lambda row: tuple(sorted(row)),
    ):
        batch = list(group)
        if skip_duplicates:
            statement = postgresql.insert(table).values(batch).on_conflict_do_nothing()
        else:
            statement = sa.insert(table).values(batch)
        inserted += len(conn.execute(statement.returning(*table.primary_key.columns)).fetchall())
    return inserted


# ---------------------------------------------------------------------------
# upsert
# ---------------------------------------------------------------------------


def upsert(
    conn: sa.Connection,
    table: sa.Table,
    model: str,
    where: Mapping[str, Any],
    create: Mapping[str, Any],
    update: Mapping[str, Any],
) -> Any:
    """``db.<model>.upsert(where=where, data={'create': create, 'update': update})``.

    Returns the row, on either branch, as a ``RowMapping``.

    **Precondition, and it is not a style rule:** ``create`` must give the fields
    in ``where`` the values ``where`` gives them. ``ON CONFLICT`` keys on the row
    being *inserted*; Prisma's ``where`` selects the row independently. Once the
    two disagree they address different rows — measured, with an existing row at
    ``slug='left'`` and ``create`` naming ``slug='right'``, Prisma round-trips
    and updates the existing row while this inserts a second one. If ``create``
    disagrees with ``where``, **STOP**: this is not a translation of what Prisma
    does.

    Also STOP for ``update={}`` (Prisma writes nothing at all — ``@updatedAt``
    does not move — while ``values_for_update({})`` still stamps it, and on a
    model without one the ``set_`` is empty and SQLAlchemy refuses to compile),
    for a nested write in either payload, for an atomic operation in ``update``,
    and for ``include=``.

    One ``moment`` goes to both halves: the engine binds a single instant, and
    the ``updatedAt`` in ``DO UPDATE SET`` is the same bind as the ``createdAt``
    in ``VALUES``.

    ``values_for_create`` generates an id on every call and the update branch
    discards it — as Prisma does. Do not try to avoid that; the ``VALUES`` clause
    needs it.
    """
    moment = datetime.datetime.now(datetime.timezone.utc)
    statement = (
        postgresql.insert(table)
        .values(**values_for_create(model, create, moment=moment))
        .on_conflict_do_update(
            index_elements=list(conflict_target(model, where)),
            set_=values_for_update(model, update, moment=moment),
        )
        .returning(*table.c)
    )
    return conn.execute(statement).mappings().one()


def conflict_target(model: str, where: Mapping[str, Any]) -> List[str]:
    """The columns ``ON CONFLICT`` needs, from the one unique ``where`` names.

    Read from the schema, never derived by splitting the constraint name on
    ``_`` — that breaks on any field containing an underscore — and never from
    ``fields``, which differs from ``columns`` under ``@map``.
    """
    if len(where) != 1:
        raise LookupError(f'{model}: a `where` for upsert names exactly one unique')

    (key,) = where
    spec = model_schema(model)

    field = spec['fields'].get(key)
    if field is not None and (field['is_id'] or field['is_unique']):
        return [field['column']]  # single @id / @unique, @map resolved

    for unique in spec['uniques']:
        if unique['name'] == key:
            return unique['columns']  # compound @@unique

    if spec['primary_key']['name'] == key:
        return spec['primary_key']['columns']  # compound @@id — NOT in ['uniques']

    raise LookupError(f'{model}.{key} is not a unique constraint')


# ---------------------------------------------------------------------------
# where by a compound key
# ---------------------------------------------------------------------------


def unique_where(model: str, table: sa.Table, where: Mapping[str, Any]) -> Any:
    """Translate a Prisma unique ``where`` into a SQLAlchemy predicate.

    Handles the flat form (``{'id': x}``) and the compound form, which Prisma
    addresses by the *constraint* identifier: ``{'siteId_slug': {'siteId': a,
    'slug': b}}`` for a compound ``@@unique`` and the same shape for a compound
    ``@@id``, which lives under ``primary_key`` and not under ``uniques``.
    """
    if len(where) != 1:
        raise LookupError(f'{model}: a unique `where` names exactly one unique')

    (key,) = where
    value = where[key]
    spec = model_schema(model)

    field = spec['fields'].get(key)
    if field is not None:
        return table.c[field['column']] == value

    members = [unique['columns'] for unique in spec['uniques'] if unique['name'] == key]
    fields = [unique['fields'] for unique in spec['uniques'] if unique['name'] == key]
    if not members and spec['primary_key']['name'] == key:
        members = [spec['primary_key']['columns']]
        fields = [spec['primary_key']['fields']]
    if not members:
        raise LookupError(f'{model}.{key} is not a unique constraint')

    return sa.and_(*(table.c[column] == value[name] for name, column in zip(fields[0], members[0])))


# ---------------------------------------------------------------------------
# relation filters
# ---------------------------------------------------------------------------


def relation_exists(
    model: str,
    field_name: str,
    subject: sa.Table,
    related: sa.Table,
    predicate: Any,
) -> Any:
    """The correlated ``EXISTS`` behind ``{'<field_name>': {'some': predicate}}``.

    ``model``/``subject`` are the model being filtered and its table;
    ``field_name``/``related`` are the relation field and the table it points at.
    For ``db.post.find_many(where={'comments': {'some': C}})`` that is
    ``relation_exists('Post', 'comments', post, comment, C)``; for
    ``{'author': {'is': C}}`` it is ``relation_exists('Post', 'author', post, user, C)``.

    A relation filter is **never** a join: a join multiplies parent rows by
    matching children and changes ``LIMIT`` semantics. The foreign key column to
    correlate on is not guessable from the field name, so it is read.

    ``none`` is ``~relation_exists(...)``; ``every`` is
    ``~relation_exists(..., sa.not_(predicate))`` — use ``sa.not_``, **not**
    ``predicate.isnot(True)``. Against a child row whose predicate evaluates to
    NULL, Prisma and ``sa.not_`` match the parent and ``isnot(True)`` does not.
    The two agree whenever the predicate cannot be NULL, so a test on
    non-nullable columns will not catch the difference.
    """
    rel = relation(model, field_name)
    if rel['join_ambiguous']:
        raise LookupError(
            f'{model}.{field_name} is a self-referential implicit many-to-many; which side is join column `A` '
            'is not recoverable from the DMMF. STOP.'
        )
    if rel['shape'] == 'many-to-many' or rel['fk_model'] is None:
        # an implicit m2m has no foreign key columns to correlate on; it needs
        # `prisma_sa.join_table_for(model, field_name)` and a two-hop EXISTS,
        # which §4.1 does not have a row for
        raise LookupError(
            f'{model}.{field_name} is many-to-many; no §4.1 row covers a relation filter across a join table. STOP.'
        )

    # whichever table holds the foreign key columns is the left of the equality
    holder_is_related = rel['fk_model'] != model
    left = related if holder_is_related else subject
    right = subject if holder_is_related else related

    correlation = sa.and_(
        *(left.c[fk] == right.c[referenced] for fk, referenced in zip(rel['fk_columns'], rel['referenced_columns']))
    )
    return sa.exists().where(sa.and_(correlation, predicate))


# ---------------------------------------------------------------------------
# comparing the two clients while both are present
# ---------------------------------------------------------------------------


def normalise(row: Mapping[str, Any]) -> Dict[str, Any]:
    """Make a ``RETURNING`` row comparable to what the Prisma client returns.

    The row is identical; the Python objects are not. Prisma parses the engine's
    response and SQLAlchemy decodes the column type, so an enum comes back as the
    **stored label** rather than the generated member (which differ under
    ``@map``) and a ``timestamp without time zone`` comes back **naive** rather
    than aware UTC.

    This makes the datetime side comparable. The enum side needs the call site's
    own enum class — ``Role(row['role'])`` — because only the call site knows
    which one.
    """
    out: Dict[str, Any] = dict(row)
    for key, value in out.items():
        if isinstance(value, datetime.datetime) and value.tzinfo is None:
            out[key] = value.replace(tzinfo=datetime.timezone.utc)
    return out


def table_for(model: str) -> sa.Table:
    """``prisma_sa.table_for``, re-exported so a call site imports one module.

    Takes the **model** name (``'Post'``), not the table name; ``@@map`` is
    exactly what it resolves. Passing the table name raises ``LookupError``.
    """
    return prisma_sa.table_for(model)
