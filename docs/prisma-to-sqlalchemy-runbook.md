# Prisma → SQLAlchemy migration runbook

**Audience: an automated agent performing the migration.** Follow the phases in
order. Do not skip verification steps. Do not improvise a translation that is
not in this document — if you cannot find one, that is a **STOP**, and STOP
means hand back to a human with the specific reason, not guess.

Every translation below was executed against a live PostgreSQL database with
both Prisma and SQLAlchemy and the results compared row for row. The harness is
`benchmarks/pg-lab/verify_translations.py` and it is the source of truth for
this file. A translation not in the table has not been verified and must not be
emitted.

---

## 0. Scope check — run this first

**Locate the schema first.** It may be one file or a directory: with
`prismaSchemaFolder` enabled it is a folder of `.prisma` files, all of which
count. Every grep in this runbook must cover all of them.

```bash
SCHEMA=$(grep -rl '^datasource' --include='*.prisma' . | head -1)
SCHEMA_DIR=$(dirname "$SCHEMA")
# use "$SCHEMA_DIR"/*.prisma below, not a single file
```

A schema folder usually means **several generator blocks**. Find which one the
application actually imports before touching any of them:

```bash
grep -rnE '^generator\s+\w+' "$SCHEMA_DIR"/*.prisma
grep -rn 'from prisma import\|import prisma' --include='*.py' . | head
```

If any answer below is "no", **STOP** and report which one.

| Check | How | Required |
| --- | --- | --- |
| Provider is PostgreSQL | `datasource.provider` | `postgresql` |
| `relationMode` is not `"prisma"` | grep `relationMode` | absent, or `foreignKeys` |
| `prisma.sa` is present **and works** | see below | yes |
| No MongoDB-only features | grep `type ` blocks | none |

```bash
# not just importable — importable *and* able to build
python -c "
from prisma import sa
print('prisma.sa', sa.__version__)
print(len(sa.metadata().tables), 'tables')"
```

Checking that `prisma.sa` merely *imports* is not enough: it imports cleanly on
a client whose metadata cannot be built, so the check passes and Phase 2 then
fails. `sa.__version__` also distinguishes this fork from upstream, which
reports the same `prisma.__version__`.

**Why the first two are hard stops:**

- **Other providers** raise `UnsupportedProviderError`. The type table has only
  been verified against PostgreSQL, and an unverified type mapping fails as a
  schema diff during a deploy, not here.
- **`relationMode = "prisma"`** means the database has **no foreign keys at
  all**. Prisma does not report this through the generator protocol, so the
  metadata cannot see it, and every FK this migration creates would be a diff
  against every table.

`@db.*` native types are **no longer a stop** — they are read from the raw
schema text and honoured. Record the count anyway, because it tells you how much
of the schema depends on the lexer:

```bash
grep -rho '@db\.[A-Za-z]*' "$SCHEMA_DIR"/*.prisma | sort | uniq -c | sort -rn
```

If any annotation in that list is not in `prisma/sa/_types.py`, `sa.metadata()`
raises naming it rather than silently falling back to the default type. That is
a STOP: report the annotation.

Record the answers. They go in the migration report.

---

## 1. Inventory — measure before changing anything

Run these and record the counts. They determine how much of phase 4 is
mechanical and how much is a STOP.

```bash
# every Prisma call site
grep -rnE '\.(find_unique|find_first|find_many|create|create_many|update|update_many|upsert|delete|delete_many|count|group_by|aggregate|query_raw|execute_raw)\(' --include='*.py' .

# the three entry syntaxes, which all have to keep working
grep -rn 'prisma\.register\|get_client()' --include='*.py' .   # registry
grep -rnE '\.prisma\(\)' --include='*.py' .                    # Model.prisma()
grep -rn '\.tx(' --include='*.py' .                            # transactions

# operations with no verified translation (see §5)
grep -rnE '\b(aggregate|group_by)\(' --include='*.py' .
grep -rn 'connectOrCreate\|disconnect\|updateMany\|deleteMany' --include='*.py' .
```

---

## 2. Enable schema metadata

```prisma
generator client {
  provider       = "prisma-client-py"
  schemaMetadata = true
}
```

```bash
prisma generate
```

Set it in the **generator block**, not as an environment variable.
`PRISMA_PY_CONFIG_SCHEMA_METADATA=1` is honoured too, but environment variables
apply to the whole process, so every generator block in the schema gets the
payload — including clients that never opted in and do not want the weight.

Two more things about multi-generator schemas:

- `metadata.py` is emitted for every Python client regardless; the flag only
  controls whether the schema payload is in it. A client without the flag gets
  a ~300 byte file, with it a payload proportional to the schema.
- If an editable install (`pip install -e`) is in play, `prisma generate`
  without an explicit `output` writes into the *library* checkout rather than
  your project. Set `output` explicitly on every generator block.

**Verify** — this must print `True` and a table name, or STOP:

```bash
python -c "from prisma import sa; print(sa.metadata() is not None, sa.table_for('<AnyModel>').name)"
```

If it raises `SchemaNotAvailableError`, the option did not take effect — check
you edited the generator block that actually runs, and re-run `prisma generate`.

---

## 3. Hand the schema to Alembic

This phase changes no application code. It transfers ownership of DDL.

```bash
pip install alembic
alembic init migrations
```

In `migrations/env.py`, replace the `target_metadata` line:

```python
from prisma import sa
target_metadata = sa.metadata()
```

Also tell Alembic to leave Prisma's own bookkeeping alone. `_prisma_migrations`
is not in the metadata, so autogenerate proposes `DROP TABLE _prisma_migrations`
— and that table is the Prisma migration history you still need for the whole
transition:

```python
def include_object(object, name, type_, reflected, compare_to):
    if type_ == 'table' and name == '_prisma_migrations':
        return False
    return True

context.configure(..., include_object=include_object)
```

**Verify — the gate for this whole phase.** Autogenerate against the existing
Prisma-managed database must produce an **empty** migration:

```bash
alembic revision --autogenerate -m "baseline"
```

Open the generated file. `upgrade()` must contain only `pass`.

- **Empty** → correct. `alembic stamp head`, and Alembic owns DDL from here.
- **Not empty** → **STOP**. Do not edit the migration to make it empty. A
  non-empty diff means the metadata and the database disagree, and applying it
  would alter a production schema. Report the diff verbatim.

Note that an empty autogenerate diff is *necessary but not sufficient* —
Alembic does not compare foreign key `ondelete`/`onupdate`, constraint names,
index methods, column order, **server defaults, or sequences**.

The last two are the ones that bite. `compare_server_default` is off by
default, so a column that lost its `DEFAULT nextval(...)` produces an empty diff
and then rejects every INSERT. Turn it on:

```python
context.configure(..., compare_type=True, compare_server_default=True)
```

For a stronger check still, if you have a scratch database available:

```bash
# build a second database from the metadata and compare DDL directly
python -c "
from prisma import sa; import sqlalchemy
sa.metadata().create_all(sqlalchemy.create_engine('$SCRATCH_URL'))"
pg_dump --schema-only --no-owner --no-acl <production> > /tmp/a.sql
pg_dump --schema-only --no-owner --no-acl <scratch>    > /tmp/b.sql
diff /tmp/a.sql /tmp/b.sql
```

Compare against a database built by `prisma migrate deploy`, and expect some
noise that is **not** the library's: run `prisma migrate diff` first to measure
your own schema-versus-migrations drift, and subtract it.

**Do not run `prisma migrate` and `alembic upgrade` against the same database.**
`_prisma_migrations` and `alembic_version` are independent and neither knows
about the other.

---

## 4. Translate queries

Set up a connection alongside the Prisma client — do not remove Prisma yet:

```python
import sqlalchemy as sa
from prisma import sa as prisma_sa

engine = sa.create_engine(DATABASE_URL)     # see the driver note below
md = prisma_sa.metadata()
post = prisma_sa.table_for('Post')          # takes the MODEL name; @@map is resolved
```

**Use the driver the application already has.** SQLAlchemy defaults
`postgresql://` to psycopg2. Do not rewrite the URL to `postgresql+psycopg://`
unless psycopg 3 is installed — most applications ship `psycopg2-binary`, and
the rewrite gives `ModuleNotFoundError: No module named 'psycopg'`:

```python
import importlib.util
if DATABASE_URL.startswith('postgresql://') and importlib.util.find_spec('psycopg2') is None:
    DATABASE_URL = DATABASE_URL.replace('postgresql://', 'postgresql+psycopg://', 1)
```

`table_for` takes the **model** name (`'Post'`), not the table name. Passing the
table name raises `LookupError`.

Migrate **one call site at a time**, and after each one assert the new result
equals the old:

```python
old = db.post.find_many(where=..., take=...)
new = conn.execute(sa.select(post).where(...).limit(...)).mappings().all()
assert sorted(p.id for p in old) == sorted(r['id'] for r in new)
```

### 4.1 Verified translations

`conn` is a `sqlalchemy.Connection`. `post`, `user`, `comment` are `Table`
objects from `table_for`.

#### Reads

| Prisma | SQLAlchemy Core |
| --- | --- |
| `db.post.find_unique(where={'id': x})` | `conn.execute(sa.select(post).where(post.c.id == x)).mappings().first()` |
| `db.post.find_unique(where={'siteId_slug': {'siteId': a, 'slug': b}})` | `...where(sa.and_(post.c.siteId == a, post.c.slug == b))` |
| `db.post.find_first(order={'createdAt': 'desc'})` | `sa.select(post).order_by(post.c.createdAt.desc()).limit(1)` |
| `db.post.find_many(take=5, skip=10, order={'id': 'asc'})` | `sa.select(post).order_by(post.c.id.asc()).limit(5).offset(10)` |
| `order=[{'a': 'asc'}, {'b': 'desc'}]` | `.order_by(t.c.a.asc(), t.c.b.desc())` |
| `distinct=['status']` | `sa.select(post.c.status).distinct()` |

A compound unique is addressed in Prisma by its **constraint name**
(`siteId_slug`); read the member fields from
`prisma._schema.model_schema('Post')['uniques']` rather than splitting the name
on `_`, which breaks on any field containing an underscore.

#### Where operators

| Prisma | SQLAlchemy |
| --- | --- |
| `{'f': v}` | `t.c.f == v` |
| `{'f': {'not': v}}` | `t.c.f != v` |
| `{'f': {'in': [...]}}` | `t.c.f.in_([...])` |
| `{'f': {'lt': v}}` / `lte` / `gt` / `gte` | `t.c.f < v` / `<=` / `>` / `>=` |
| `{'f': {'contains': s}}` | `t.c.f.like(f'%{s}%')` |
| `{'f': {'startsWith': s}}` | `t.c.f.like(f'{s}%')` |
| `{'f': {'endsWith': s}}` | `t.c.f.like(f'%{s}')` |
| `{'f': {'contains': s, 'mode': 'insensitive'}}` | `t.c.f.ilike(f'%{s}%')` |
| `{'f': None}` | `t.c.f.is_(None)` — **not** `== None` |
| `{'f': {'not': None}}` | `t.c.f.is_not(None)` |
| `{'AND': [a, b]}` | `sa.and_(a, b)` |
| `{'OR': [a, b]}` | `sa.or_(a, b)` |
| `{'NOT': [a]}` | `sa.not_(a)` |

For `contains`/`startsWith`/`endsWith`, escape `%` and `_` in the user-supplied
string, or a value containing them silently matches more rows than Prisma would.

#### Aggregates

| Prisma | SQLAlchemy |
| --- | --- |
| `db.post.count()` | `sa.select(sa.func.count()).select_from(post)` |
| `db.comment.count(where={'isHidden': False})` | `...select_from(comment).where(comment.c.isHidden.is_(False))` |
| `db.post.group_by(['status'], count={'id': True})` | `sa.select(post.c.status, sa.func.count(post.c.id)).group_by(post.c.status)` |

Prisma returns group-by rows as `{'status': ..., '_count': {'id': n}}`. The
SQLAlchemy row is flat — reshape at the call site.

#### Relation filters

All three compile to a correlated `EXISTS`, never a join. A join multiplies
parent rows by matching children and changes `LIMIT` semantics.

| Prisma | SQLAlchemy |
| --- | --- |
| `{'comments': {'some': C}}` | `sa.exists().where(sa.and_(comment.c.postId == post.c.id, C))` |
| `{'comments': {'none': C}}` | `~sa.exists().where(sa.and_(comment.c.postId == post.c.id, C))` |
| `{'comments': {'every': C}}` | `~sa.exists().where(sa.and_(comment.c.postId == post.c.id, sa.not_(C)))` |
| `{'author': {'is': C}}` | `sa.exists().where(sa.and_(user.c.id == post.c.authorId, C))` |

**`every` — read this before writing one.** Use `sa.not_(C)`. Do **not** use
`(C).isnot(True)`, even though it is the set-theoretically defensible reading.
Measured against the engine, with a child row whose predicate evaluates to NULL:

| | parent matches? |
| --- | --- |
| Prisma | True |
| `sa.not_(C)` | True ✅ |
| `(C).isnot(True)` | False ❌ |

Prisma treats a child whose predicate is UNKNOWN as **not a violation**. The two
forms agree whenever `C` cannot be NULL, so a test written against non-nullable
columns will not catch the difference.

The foreign key column to correlate on is **not** guessable from the field name.
Read it:

```python
from prisma._schema import relation
rel = relation('Post', 'comments')
# rel['fk_model'], rel['fk_columns'], rel['referenced_columns']
```

#### Includes

Prisma's `include` returns nested objects. SQLAlchemy Core returns flat rows.
There is no mechanical equivalent — pick a shape:

| Prisma | SQLAlchemy |
| --- | --- |
| `include={'author': True}` (to-one) | `sa.select(post, user).join(user, user.c.id == post.c.authorId)` |
| `include={'comments': True}` (to-many) | two queries: parents, then `comment.c.postId.in_(parent_ids)`, grouped in Python |

Do **not** emulate a to-many `include` with a single join. It multiplies parent
rows by child count, breaks `take`/`skip`, and needs de-duplication in Python
that costs more than the second query.

For an **explicit** m2m (a join model in the schema, like `PostTag`), it is an
ordinary two-hop join:

```python
sa.select(tag.c.name).select_from(post_tag.join(tag, tag.c.id == post_tag.c.tagId)).where(post_tag.c.postId == x)
```

For an **implicit** m2m (`Tag[]` on both sides, no join model), get the hidden
join table from `prisma_sa.join_table_for('Post', 'tags')` — it has no model, so
`table_for` cannot reach it.

---

## 5. STOP list — no verified translation

Encountering any of these means STOP for that call site. Leave the Prisma call
in place, record it in the report, and continue with the others.

| Operation | Why |
| --- | --- |
| `create`, `create_many`, `update`, `update_many`, `upsert`, `delete`, `delete_many` | Not verified. Writes involve client-side default generation (`cuid()`, `uuid()`, `@updatedAt`) that Prisma fills in and SQLAlchemy will not. |
| Nested writes (`create: {..., posts: {create: [...]}}`) | Requires a recursive planner. Ordering, FK satisfaction and rollback are all unsolved here. |
| `connect` / `disconnect` / `set` / `connectOrCreate` | `disconnect` and `set` are **illegal** against a NOT NULL foreign key — Prisma raises P2014. Check `relation(...)['fk_required']` before assuming otherwise. |
| `aggregate()` (`_sum`, `_avg`, `_min`, `_max`) | Not verified. |
| `db.tx()` / transactions | Prisma's transaction semantics and SQLAlchemy's do not map one-to-one. |
| Self-referential implicit m2m | Which side is join column `A` is not recoverable from the DMMF. `relation(...)['join_ambiguous']` is `True`; traversal would be a coin flip. It is `False` on every other relation, so the check is always safe to make. |
| `Json` field filtering (`path`, `string_contains`) | Not verified. |
| Scalar list filters (`has`, `hasEvery`, `hasSome`) | Not verified. |
| Full-text search (`search`) | Not verified. |
| Atomic updates (`{'increment': 1}`) | Not verified. |

`query_raw` / `execute_raw` need no translation — pass the same SQL to
`conn.execute(sa.text(...))`. Convert positional `$1` parameters to named
`:name` bind parameters.

---

## 6. Report

Produce this at the end. It is the deliverable, not the code diff.

```markdown
## Migration report

Scope check: <provider> / relationMode <value> / @db.* usage: <count>
Alembic baseline: <empty | NOT EMPTY — diff below>

Call sites: <n> found, <n> translated, <n> left on Prisma

### Translated
<file:line — operation — verified equal: yes/no>

### Left on Prisma (STOP)
<file:line — operation — reason from §5>

### Requires human decision
<anything the scope check flagged>
```

Do not report the migration as complete while any call site is still on Prisma.
Report it as partial, with the count.

---

## What does not exist yet

Stated so an agent does not go looking for it:

- **No CLI.** There is no `prisma py sqlalchemy generate/baseline/verify/handover/doctor`.
  Every step above is manual.
- **No declarative models.** `prisma.sa` produces `MetaData` and `Table`
  objects (SQLAlchemy Core), not declarative classes. There is no source to
  check in.
- **No query compiler.** Prisma calls are not automatically redirected;
  §4 is a hand translation.
- **No verified write translations.** §5 lists them as STOP. On a typical
  application that is the majority of call sites — one field report measured
  62% — so expect Phase 3 (Alembic owning DDL) to be the deliverable and Phase 4
  to be partial.
- **PostgreSQL only.**

### Prisma CLI version

The library pins the Prisma CLI version it was built against; `prisma.config`
holds it. An unpinned `npx prisma` resolves to whatever is current and will
reject the schema with `P1012` and no hint that the cause is a version skew:

```bash
python -c "from prisma import config; print(config.prisma_version)"
```

Pin the same version in `package.json`, and use `python -m prisma` rather than
`npx prisma` so the pinned CLI is the one that runs.

---

## Changelog

**1.2.0** — three more from the retest of 1.1.0, on the same schema.

| | fixed |
| --- | --- |
| foreign key names not truncated at 63 characters — the half of B3 the first fix missed | ✅ |
| `sort: Desc` dropped from index columns, changing which queries the index can serve | ✅ |
| `@relation(map: "…")` ignored, so the constraint took a derived name | ✅ |

**1.1.0** — five defects fixed from a field report against a 182-model
production schema. All five are now covered by the reference schema and the
DDL-equivalence gate.

| | fixed |
| --- | --- |
| `@default(uuid())` reaching the metadata as `uuid(4)` and blocking every schema using it | ✅ |
| non-primary-key `@default(autoincrement())` silently losing its sequence, so every INSERT failed | ✅ |
| `@db.*` native types being invisible — now lexed from the raw schema text and honoured | ✅ |
| derived constraint names exceeding PostgreSQL's 63-character limit | ✅ |
| empty scalar-list defaults emitting an uncastable `ARRAY[]` | ✅ |

`prisma.sa.__version__` reports this.
