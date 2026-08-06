# §4.1 verified translations, and §5

Condensed from `docs/prisma-to-sqlalchemy-runbook.md`. **The runbook is the
authority** — read the real §4.1 when you have it. This file exists so that a
missing runbook degrades the migration rather than stopping it, and so the rows
you need most are one file away.

Every row below was executed against a live PostgreSQL database with both
Prisma and SQLAlchemy and compared row for row. **A translation not in this
table has not been verified and must not be emitted.**

Setup:

```python
import sqlalchemy as sa
from prisma import sa as prisma_sa
from prisma.sa import values_for_create, values_for_update
from prisma._schema import model_schema, relation, enum_label

engine = sa.create_engine(DATABASE_URL)     # driver note below
post = prisma_sa.table_for('Post')          # the MODEL name; @@map is resolved
```

`table_for` takes the **model** name. Passing the table name raises
`LookupError`. `join_table_for('Post', 'tags')` reaches the hidden join table of
an implicit m2m, which has no model.

**Driver.** SQLAlchemy defaults `postgresql://` to psycopg2. Do not rewrite the
URL to `postgresql+psycopg://` unless psycopg 3 is actually installed — most
applications ship `psycopg2-binary` and the rewrite gives
`ModuleNotFoundError: No module named 'psycopg'`.

---

## Reads

| Prisma | SQLAlchemy Core |
| --- | --- |
| `db.post.find_unique(where={'id': x})` | `conn.execute(sa.select(post).where(post.c.id == x)).mappings().first()` |
| `find_unique(where={'siteId_slug': {'siteId': a, 'slug': b}})` | `...where(sa.and_(post.c.siteId == a, post.c.slug == b))` |
| `find_first(order={'createdAt': 'desc'})` | `sa.select(post).order_by(post.c.createdAt.desc()).limit(1)` |
| `find_many(take=5, skip=10, order={'id': 'asc'})` | `sa.select(post).order_by(post.c.id.asc()).limit(5).offset(10)` |
| `order=[{'a': 'asc'}, {'b': 'desc'}]` | `.order_by(t.c.a.asc(), t.c.b.desc())` |
| `distinct=['status']` | `sa.select(post.c.status).distinct()` |

A compound unique is addressed by its **constraint name** (`siteId_slug`). Read
the member columns; never split the name on `_`, which breaks on any field
containing an underscore:

```python
next(u for u in model_schema('Post')['uniques'] if u['name'] == 'siteId_slug')['columns']
model_schema('Composite')['primary_key']['columns']   # a compound @@id is NOT in ['uniques']
```

Use `columns`, never `fields` — under `@map` they differ, and `post.c['slug']`
does not exist when the column is `url_slug`.

## Where operators

| Prisma | SQLAlchemy |
| --- | --- |
| `{'f': v}` | `t.c.f == v` |
| `{'f': {'not': v}}` | `t.c.f != v` |
| `{'f': {'in': [...]}}` | `t.c.f.in_([...])` |
| `{'f': {'lt'/'lte'/'gt'/'gte': v}}` | `t.c.f < v` / `<=` / `>` / `>=` |
| `{'f': {'contains': s}}` | `t.c.f.like(f'%{s}%')` |
| `{'f': {'startsWith': s}}` | `t.c.f.like(f'{s}%')` |
| `{'f': {'endsWith': s}}` | `t.c.f.like(f'%{s}')` |
| `{'f': {'contains': s, 'mode': 'insensitive'}}` | `t.c.f.ilike(f'%{s}%')` |
| `{'f': None}` | `t.c.f.is_(None)` — **not** `== None` |
| `{'f': {'not': None}}` | `t.c.f.is_not(None)` |
| `{'AND': [a, b]}` | `sa.and_(a, b)` |
| `{'OR': [a, b]}` | `sa.or_(a, b)` |
| `{'NOT': [a]}` | `sa.not_(a)` |

Escape `%` and `_` in user-supplied strings for
`contains`/`startsWith`/`endsWith`, or a value containing them silently matches
more rows than Prisma would.

`equals`, `not_in`/`notIn`, `is_not`/`isNot`, `is_empty`/`isEmpty` have **no**
§4.1 row. The doctor reports them `where-operator-not-covered` → unknown.

## Writes — one row, scalar columns

`data` may hold scalar and enum fields only. Anything naming a relation is §5,
and `values_for_create` raises `LookupError` naming the field rather than
guessing. `D` is the `data` mapping unchanged.

| Prisma | SQLAlchemy Core |
| --- | --- |
| `db.post.create(data=D)` | `conn.execute(sa.insert(post).values(**values_for_create('Post', D)))` |
| ...using the returned row | `.returning(*post.c)` → `.mappings().one()` |
| `db.post.update(where={'id': x}, data=D)` | `sa.update(post).where(post.c.id == x).values(**values_for_update('Post', D))` |
| ...using the returned row | `.returning(*post.c)` → `.mappings().first()` |
| `db.post.delete(where={'id': x})` | `conn.execute(sa.delete(post).where(post.c.id == x))` |
| ...using the returned row | `.returning(*post.c)` → `.mappings().first()` |

**Do not write the INSERT by hand.** `@default(cuid())` and `@updatedAt` leave no
trace in the DDL, so a bare `sa.insert(post).values(slug=..., title=...)` sends
no id and no `updatedAt`. SQLAlchemy warns — *"Column 'Post.id' is marked as a
member of the primary key … but has no … default generator"* — and PostgreSQL
rejects the row. The Phase 2 Alembic gate cannot catch this: nothing about the
schema is wrong. This holds for the **generated declarative models too** — they
carry `server_default` where the database has one and nothing at all where
Prisma filled the value in client-side.

**`.first()`, not `.one()`.** `update`/`delete` return `None` on no match;
`prisma-client-py` catches `RecordNotFoundError` and a call site may rely on it.
`.one()` raises instead. Nothing is written on a miss.

**Pass a naive UTC datetime for an explicit `DateTime`.** `values_for_*`
normalises the timestamps it *generates*; one the caller supplies passes through,
and PostgreSQL converts an aware value into `timestamp without time zone` using
the session's `TimeZone`. Prisma never does. Measured at `America/New_York` with
`2020-06-01T12:00Z`: Prisma stores `12:00`, an aware value through
`values_for_create` stores `08:00`, a naive UTC value stores `12:00`.

**A unique violation raises a different class.**
`prisma.errors.UniqueViolationError` → `sqlalchemy.exc.IntegrityError`. The only
portable identification is `exc.orig.sqlstate == '23505'`. Neither writes a
partial row.

**`ON DELETE` is the database's.** A flat `sa.delete(...)` cascades exactly as
Prisma's does — the foreign key performs it either way. There is no client-side
child deletion to reproduce.

**Return values: same row, different Python objects.**

| column | Prisma | a `RETURNING` row |
| --- | --- | --- |
| enum | the generated member, `Role.ADMIN` | the stored label, `'administrator'` |
| `DateTime` | aware, UTC | naive, for `timestamp without time zone` |

Under `@map` those enum strings genuinely differ, so a comparison that used to
match stops matching. Timestamps need
`value.astimezone(timezone.utc).replace(tzinfo=None)` before comparing.

## Bulk writes

All three return a **count**, never rows.

| Prisma | SQLAlchemy |
| --- | --- |
| `db.post.create_many(data=[d1, d2])` | `insert_many(conn, post, 'Post', [d1, d2])` — `assets/sa_helpers.py` |
| `create_many(data=[...], skip_duplicates=True)` | `insert_many(..., skip_duplicates=True)` → `ON CONFLICT DO NOTHING` |
| `db.post.update_many(where=W, data=D)` | `conn.execute(sa.update(post).where(W').values(**values_for_update('Post', D))).rowcount` |
| `db.post.delete_many(where=W)` | `conn.execute(sa.delete(post).where(W')).rowcount` |
| `db.post.delete_many()` (no where) | `conn.execute(sa.delete(post)).rowcount` — empties the table |

`W'` is the where-operator translation above; nothing about it changes for a
write.

Every line of `insert_many` is load-bearing:

- **One `moment` for the whole batch.** `create_many` stamps every row with a
  *single* instant and gives each its **own** id. Calling `values_for_create`
  per row without `moment=` reads the clock per row — invisible in a single-row
  test, visible as rows of one batch disagreeing about `createdAt`.
- **One INSERT per distinct key set.** `sa.insert(t).values([...])` compiles its
  `VALUES` from the **first** mapping, so a key only later rows carry is dropped
  — silently, with the right row count. Same for `conn.execute(sa.insert(t), [...])`.
- **The count comes from `RETURNING`.** `rowcount` is `-1` for INSERT under
  psycopg. `.returning(*table.primary_key.columns)` + `len(...)` is the count,
  and it is what makes `skip_duplicates` countable — `RETURNING` on
  `ON CONFLICT DO NOTHING` yields only the rows actually inserted. `rowcount`
  **is** correct for UPDATE and DELETE.
- **Wrap the batch in a transaction.** Prisma's `create_many` is one unit;
  `insert_many` is one statement per key set, so on autocommit a failure in the
  second group leaves the first committed.

`skip_duplicates` matches Prisma both against an already-present row and against
a duplicate appearing twice *inside* the batch. Without it, a conflict aborts
the whole batch and inserts nothing.

`update_many` **counts matched rows, not changed rows, and stamps all of them**:
`@updatedAt` moves on every matched row including unchanged ones, and the whole
matched set gets one instant because it is one statement. `createdAt` untouched.
Zero matched returns `0` and moves nothing.

**A `where` on a `@map`ped enum needs translating.** Prisma addresses a member by
its schema name; the column only ever holds the mapped label, and binding
`'ADMIN'` is rejected by the enum type outright:

```python
accounts.c.role == enum_label('Role', 'ADMIN')   # -> 'administrator'
```

## Upsert

Verified **only under the precondition**: `create` must give the fields in
`where` the values `where` gives them. Outside it Prisma stops compiling a single
`INSERT … ON CONFLICT` and round-trips instead, and the two are not the same
operation — measured, with a row at `slug='left'` and `create` naming
`slug='right'`, Prisma updates the existing row and `on_conflict_do_update`
inserts a second one. **If `create` disagrees with `where`, STOP.**

Use `upsert()` / `conflict_target()` from `assets/sa_helpers.py`. Conflict target
by `where` shape:

| `where` | conflict target | read from |
| --- | --- | --- |
| `{'id': x}` | `['id']` | `model_schema(M)['fields'][key]['column']` |
| `{'email': x}` single `@unique` | `['email']` (`@map` resolved) | same |
| `{'siteId_slug': {...}}` compound `@@unique` | `['siteId', 'slug']` | `model_schema(M)['uniques']` |
| `{'day_siteId': {...}}` compound `@@id` | `['day', 'siteId']` | `model_schema(M)['primary_key']['columns']` |

- **Pass one `moment` to both halves** — the engine binds a single instant; the
  `updatedAt` in `DO UPDATE SET` is the same bind as the `createdAt` in `VALUES`.
- **The generated id on the update branch is discarded**, as Prisma discards it.
  Do not try to avoid it; the `VALUES` clause needs it.
- **It is one statement, and that is the point.** Under a concurrent insert of
  the same key, Prisma and `on_conflict_do_update` both update the other
  writer's row; a `SELECT`-then-`INSERT/UPDATE` translation raises `23505`. It
  lands the same rows on every uncontended run, so no test will tell you it is
  wrong.
- **`ON CONFLICT (a)` says nothing about unique `b`** — a different unique
  violation raises rather than updating, as Prisma does.
- Create branch: the create payload, `createdAt == updatedAt`, update payload not
  applied. Update branch: the update payload, `id`/`createdAt` unchanged,
  `updatedAt` moves, create payload not applied.

## Transactions

| Prisma | SQLAlchemy Core |
| --- | --- |
| `async with db.tx() as tx:` | `with engine.begin() as conn:` |
| `tx = await db.tx().start()` … `await manager.rollback()` | `conn = engine.connect()`; `t = conn.begin()` … `t.rollback()` |
| a `tx()` opened inside a `tx()` | a **second** `engine.begin()` — *not* `conn.begin_nested()` |
| `async with db.batch_() as batcher:` | the same statements between one `engine.begin()` |
| `async with tx.batch_() as batcher:` | the same statements on the connection already open |

It is `engine.begin()` — not a `Session` (there are no declarative classes on the
Core path) and not a savepoint.

Rollback semantics are identical, with one code change: a handle used after the
block raises `prisma.errors.TransactionExpiredError` on one side and
`sqlalchemy.exc.ResourceClosedError` on the other, so
`except prisma.errors.TransactionExpiredError` has to change.

**A failed statement poisons the rest of the transaction on both sides** —
PostgreSQL aborts and refuses everything after it with `25P02`. A call site that
catches an error inside a `tx()` and carries on was already broken before the
migration; Prisma exposes no savepoint API. `begin_nested()` is a capability the
migration *gains*, not a translation of anything, and should not be introduced
while the two sides are still being compared.

**A `tx()` inside a `tx()` is a second, independent transaction, not a
savepoint.** Measured on visibility: neither Prisma's nested `tx()` nor a second
`engine.begin()` can see the outer block's uncommitted row; `begin_nested()`
**can**. `xmin` cannot tell them apart — PostgreSQL gives each savepoint its own
subtransaction id — so visibility is the measurement that distinguishes them.

**Migrate a `tx()` block whole, or leave all of it on Prisma.** One Prisma write
and one SQLAlchemy write inside one `with db.tx()` block: different transaction
ids, neither half sees the other, and rolling back the SQLAlchemy half leaves the
Prisma half committed. There is no way to make a Prisma client join a SQLAlchemy
transaction.

**A call site handed a `Connection` must not call `conn.begin()`.** A
`Connection` autobegins on its first statement, so a helper that becomes
`conn.begin()` raises `InvalidRequestError: … already initialized a SQLAlchemy
Transaction()` the moment its caller has already used the connection — a
request-scoped transaction, or a test fixture. Take the `Connection` as a
parameter and let the caller own the boundary; `begin_nested()` is the only inner
boundary that composes, and it works on a fresh connection too.

**`batch_()` is a transaction, not just a pipeline.** One conflict discards the
batch including the queries before it, on both sides; an empty `batch_()` writes
nothing and raises nothing; a `batch_()` inside a `tx()` joins the outer
transaction. A batch member returns `None`, so a call site inside one already
cannot read back what it wrote — nothing is lost by `conn.execute()` returning a
`CursorResult`.

**`max_wait` is `pool_timeout`, but not by default** — 2s vs 30s. Set it
deliberately on `create_engine`; never inherit it. **`timeout` is a STOP.**

## Aggregates

| Prisma | SQLAlchemy |
| --- | --- |
| `db.post.count()` | `sa.select(sa.func.count()).select_from(post)` |
| `db.comment.count(where={'isHidden': False})` | `...select_from(comment).where(comment.c.isHidden.is_(False))` |
| `db.post.group_by(['status'], count={'id': True})` | `sa.select(post.c.status, sa.func.count(post.c.id)).group_by(post.c.status)` |

Prisma returns group-by rows as `{'status': ..., '_count': {'id': n}}`; the
SQLAlchemy row is flat. Reshape at the call site. `sum`/`avg`/`min`/`max` on
`group_by` are the `aggregate()` family and are **§5**.

## Relation filters

All three compile to a correlated `EXISTS`, never a join — a join multiplies
parent rows by matching children and changes `LIMIT` semantics.

| Prisma | SQLAlchemy |
| --- | --- |
| `{'comments': {'some': C}}` | `sa.exists().where(sa.and_(comment.c.postId == post.c.id, C))` |
| `{'comments': {'none': C}}` | `~sa.exists().where(sa.and_(comment.c.postId == post.c.id, C))` |
| `{'comments': {'every': C}}` | `~sa.exists().where(sa.and_(comment.c.postId == post.c.id, sa.not_(C)))` |
| `{'author': {'is': C}}` | `sa.exists().where(sa.and_(user.c.id == post.c.authorId, C))` |

**`every` uses `sa.not_(C)`.** Do **not** use `(C).isnot(True)`, even though it
is the set-theoretically defensible reading: against a child row whose predicate
evaluates to NULL, Prisma and `sa.not_(C)` match the parent and `(C).isnot(True)`
does not. The two forms agree whenever `C` cannot be NULL, so a test on
non-nullable columns will not catch it.

The FK column to correlate on is **not** guessable from the field name:

```python
rel = relation('Post', 'comments')
rel['fk_model'], rel['fk_columns'], rel['referenced_columns']
```

## Includes

`include` returns nested objects; Core returns flat rows. There is no mechanical
equivalent — pick a shape.

| Prisma | SQLAlchemy |
| --- | --- |
| `include={'author': True}` (to-one) | `sa.select(post, user).join(user, user.c.id == post.c.authorId)` |
| `include={'comments': True}` (to-many) | two queries: parents, then `comment.c.postId.in_(parent_ids)`, grouped in Python |

Do **not** emulate a to-many `include` with a single join: it multiplies parent
rows by child count, breaks `take`/`skip`, and needs de-duplication in Python
that costs more than the second query.

Explicit m2m (a join model like `PostTag`) is an ordinary two-hop join. Implicit
m2m needs `prisma_sa.join_table_for('Post', 'tags')` — the hidden table has no
model, so `table_for` cannot reach it.

---

# §5 — the STOP list

Encountering any of these means STOP **for that call site**: leave the Prisma
call in place, record it with the reason, continue with the others.

| Operation | Why |
| --- | --- |
| `upsert` where `create` disagrees with `where`, or `update={}`, or `include=` | Prisma stops compiling one `INSERT … ON CONFLICT` and round-trips; the two are not the same operation. `update={}` writes **nothing** — `@updatedAt` does not move — while `values_for_update({})` still stamps it |
| Nested writes (`create: {..., posts: {create: [...]}}`) | needs a recursive planner; ordering, FK satisfaction and rollback are unsolved. `values_for_create` raises `LookupError` on a relation field |
| `connect` / `disconnect` / `set` / `connectOrCreate` | `disconnect`/`set` are **illegal** against a NOT NULL FK — Prisma raises P2014. Check `relation(...)['fk_required']` |
| `aggregate()` (`_sum`, `_avg`, `_min`, `_max`) | not verified |
| `db.tx(timeout=...)` | no equivalent. The query engine enforces it *between* queries with its own timer; it is neither `statement_timeout` nor `lock_timeout`. Measured against a row locked 1.2s with `timeout=200ms`: Prisma sits through the whole wait then finds the transaction expired; `SET LOCAL lock_timeout` gives up at 200ms. **Do not emit `lock_timeout`.** `db.tx()`/`db.batch_()` themselves *are* verified |
| A transaction spanning a Prisma client *and* a SQLAlchemy connection | impossible, measured — not merely unverified |
| An isolation level other than the default | `tx()` cannot request one; both sides open at READ COMMITTED |
| Self-referential implicit m2m | which side is join column `A` is not recoverable from the DMMF. `relation(...)['join_ambiguous']` is `True` there and `False` everywhere else |
| `Json` field filtering (`path`, `string_contains`) | not verified |
| Scalar list filters (`has`, `hasEvery`, `hasSome`) | not verified |
| Full-text search (`search`) | not verified |
| Atomic updates (`{'increment': 1}`) | not verified. `values_for_update` refuses them by name |

`query_raw` / `execute_raw` need no translation — the same SQL goes to
`conn.execute(sa.text(...))`. Convert positional `$1` parameters to named
`:name` binds. The doctor reports them `unknown` (`raw-sql`) rather than
`verified`, because no §4.1 row executed one and the bind conversion is real
work.
