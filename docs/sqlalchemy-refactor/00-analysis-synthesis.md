# SQLAlchemy engine refactor — analysis synthesis

Consolidated findings from six parallel refactor analyses (public API contract;
query-compiler internals; nested writes and relation filters; results/errors/
lifecycle; schema and migration tooling; conformance testing).

**Goal:** replace the Prisma binary query engine (a Rust subprocess spoken to
over HTTP/GraphQL) with SQLAlchemy, keeping the public Python API and all
returned payloads identical. No signature changes, no payload changes.

**Why:** measured on this repo's own rig, the database is ~10% of a Prisma
query and the query engine is ~90% — the same query is 8–12x slower through
Prisma than through psycopg. See [`../performance-findings.md`](../performance-findings.md).

---

## 1. The seam

Every query in the generated client reaches
`_base_client._execute(method, arguments, model, root_selection)` — **562 call
sites**. Five of the six public entry syntaxes converge there:

| syntax | reaches `_execute` |
| --- | --- |
| `db.post.find_many()` | yes |
| `Post.prisma().find_many()` | yes, via `get_client()` |
| `Post.prisma(db).find_first()` | yes |
| `tx.post.find_many()` inside `db.tx()` | yes, carries `_tx_id` |
| `Post.prisma(tx).count()` | yes, carries `_tx_id` |
| `db.batch_()` | **no** — calls `_engine.query()` directly |

**Chosen seam: implement `SyncAbstractEngine` / `AsyncAbstractEngine`**
(`engine/_abstract.py`). That abstraction already exists, so `_base_client`,
`_transactions`, `actions.py.jinja`, `client.py.jinja`, `_registry` and
`testing` need no changes at all — only `_create_engine` / `_engine_class`
(two sites each in sync and async).

The engine contract to implement in full:

1. `query(content, *, tx_id, decoder)` returning `{'data': {'result': ...}}`
2. `start_transaction` / `commit_transaction` / `rollback_transaction`
3. `connect` / `close` / `stop` lifecycle
4. `metrics(format=...)` — **no SQLAlchemy equivalent**, a documented gap

### 1.1 The seam leaks GraphQL

`count` and `group_by` build **raw GraphQL fragment strings** in the generated
actions template and pass them down as `root_selection`:

| producer | value |
| --- | --- |
| `create_many` / `update_many` / `delete_many` | `['count']` |
| `count()` | `['_count { _all }']` |
| `count(select=…)` | `['_count {a b c}']` |
| `group_by` | `[*by, '_avg {x y}', '_min {…}', '_sum {…}', '_max {…}', '_count {…}']` |

The set of producers is closed (four shapes), so the new engine can parse them.
The alternative is changing the templates to emit structured aggregate
descriptors — cleaner, but it changes generated output. `actions.py.jinja`
already carries a `# TODO: this selection building should be moved to the
QueryBuilder` acknowledging the problem.

---

## 2. Blockers, in dependency order

### 2.1 The DMMF drops what the compiler needs (Stage 0)

Verified against the actual wire payload, not the Pydantic models. Prisma sends
these; `extra='ignore'` discards them:

| gap | field | consequence |
| --- | --- | --- |
| G1 | `Field.db_name` | every `@map("col")` → guaranteed schema diff |
| G2 | `Field.native_type` | every `@db.VarChar(255)` / `@db.Uuid` collapses to the default type |
| G3 | `Datamodel.indexes` | all `@@index` / `@@fulltext` invisible |
| G4 | `Model.unique_fields` | field-order guarantees lost |
| G5 | `Datasource.relation_mode`, `.schemas` | `relationMode="prisma"` has **no FKs in the DB**; emitting them diffs every relation |

All additive and non-breaking. `relationMode` is genuinely absent from the
generator payload and must be lexed out of the raw schema text.

**Status: closed** (`df647c0`), with one correction to the table above. G2 is
not a case of us discarding what Prisma sends — there are **zero `nativeType`
keys in the entire wire payload**, verified against a schema using
`@db.VarChar(255)` and `@db.Decimal(12,2)`.

G2 is now closed by the other route: `GenericData.datamodel` carries the raw
schema text, so `generator/_native_types.py` lexes the annotations out of it and
the metadata reports them. This was forced by a field report — on a 182-model
production schema, `@db.*` covered 554 columns including every primary and
foreign key, and reading them as `text` is a full-database rewrite rather than a
post-cutover diff. See `01-field-report-response.md`.

`relationMode` is in the same position — absent from the payload — and is
**still open**. It is recoverable the same way if it becomes a priority.

`tests/test_generation/test_dmmf_completeness.py` now asserts that the modelled
field set covers the wire key set, so the next such gap fails a test instead of
surfacing months later as a mysterious schema diff.

### 2.2 Runtime metadata is insufficient

`metadata.py.jinja` emits only `PRISMA_MODELS` and `RELATIONAL_FIELD_MAPPINGS`
(model names, and `field → related model name`). **That is the entire runtime
schema knowledge of the client today**, because the engine did the rest.

The compiler additionally needs, per model: table name, per-field column name,
SQL type, nullability, default *kind* (cuid/uuid/now/autoincrement), `@updatedAt`
flag, primary key (incl. compound), unique constraints with names; and per
relation: shape (S1–S4 below), FK columns, referenced columns, relation name,
and **FK nullability** — which is what decides whether `disconnect`/`set` are
legal at all. It is all present in the generator's `Model`/`Field` objects.

**Status: closed.** `schemaMetadata = true` (off by default, so default output
is unchanged) emits `SCHEMA`, `ENUM_SCHEMA` and `DATABASE_PROVIDER` into
`metadata.py`; `prisma._schema` is the typed runtime view. Relations are
classified into four shapes — named `to-one-owner`, `to-one-inverse`, `to-many`
and `many-to-many` rather than S1–S4 — each carrying the FK model, FK columns,
referenced columns, `on_delete`, the back-relation field, and `fk_required`.

Three judgement calls worth knowing before building on it:

- **`fk_required` is reported identically on both sides of a relation**, because
  it is the same column. `Account.posts` and `Entry.account` both report `True`;
  a planner that only checks the singular side would let `disconnect` through on
  the list side and orphan rows instead of raising P2014.
- **Prisma's default primary key constraint name is provider-specific**
  (`<table>_pkey` on PostgreSQL, `PRIMARY` on MySQL), so an unmapped `@@id`
  reports `None`. Unique and index default names *are* provider-independent
  (`<table>_<cols>_key` / `_idx`) and are resolved, so a differ compares real
  names instead of reporting every index as both dropped and added.
- **Self-referential implicit m2m is flagged `join_ambiguous`, not guessed.**
  Which side is join column `A` follows model-name order, which does not break
  the tie when both sides are the same model. A coin-flip there produces a join
  that silently returns the wrong rows.

`@id`/`@@id`/`@unique`/`@@unique` are excluded from `indexes` — Prisma lists
them there as well as in the constraint fields, and emitting both would make a
migration tool create a redundant index alongside every key.

### 2.3 Removing the engine removes all validation

`find_unique` rejecting a non-unique `where`, unknown field names, wrong types —
all engine-side today, surfacing as `FieldNotFoundError` / `InputError` /
`MissingRequiredValueError`. Without it those become `KeyError` / `AttributeError`
or, worse, silently wrong queries. The actions layer's control flow depends on
this: `update()` and `delete()` return `None` by *catching* `RecordNotFoundError`.

### 2.4 Scope: the TypedDicts are fiction

`data=` is forwarded verbatim to the engine with **zero client-side validation**.
Nested `update` / `updateMany` / `deleteMany` / `upsert` / `createMany` are
commented out in `types.py.jinja` but **work today at runtime**. Any user on
`# type: ignore` or plain dicts depends on them.

This decides whether the nested-write planner must be recursive from day one.
Retrofitting recursion later is a rewrite.

---

## 3. Semantics a naive translation gets wrong

Ordered by likelihood of shipping silently.

### 3.1 `every` under three-valued logic — **this analysis had it backwards**

The original claim below was that `NOT (C)` is wrong and `(C) IS NOT TRUE` is
correct. **Measured against a live Prisma engine, the opposite is true**, and
the correction matters because it was ranked the highest-risk item here.

The experiment (`benchmarks/pg-lab/verify_translations.py`): post `p1` with two
comments, one where the predicate is TRUE and one where the predicate is NULL
because the column is NULL.

| `every: {parentId: 'cB'}` | `p1` matches? |
| --- | --- |
| Prisma engine | **True** |
| `NOT EXISTS (… AND NOT (C))` | True ✅ agrees with Prisma |
| `NOT EXISTS (… AND (C) IS NOT TRUE)` | False ❌ disagrees |

`IS NOT TRUE` is the set-theoretically defensible reading — a child whose `C` is
UNKNOWN has not been *shown* to satisfy `C`. Prisma took the other reading: an
UNKNOWN child is not a violation. A migration is judged on behavioural
equivalence with Prisma, not on which reading is nicer, so **emit `NOT (C)`**.

Both readings agree whenever `C` cannot evaluate to NULL, which is why this hid:
in the reference seed data every `parentId` was NULL, so both forms returned the
same rows and the case proved nothing. Any test for this needs a child row whose
predicate is genuinely UNKNOWN, and a paired assertion that the *other* form
disagrees — otherwise it silently goes vacuous.

`every` remains **vacuously true for zero children** under both readings.

### 3.2 Relation filters are subqueries, never joins

- `some` → `EXISTS (… AND C)`
- `none` → `NOT EXISTS (… AND C)` — parents with zero children match
- `every` → as above
- `is` → `EXISTS` on the to-one; `is: None` → `NOT EXISTS`
- `is_not` → `NOT EXISTS (… AND C)` — **a row whose FK is NULL matches**

Joining duplicates parent rows and corrupts `take`, `skip`, `count`, `distinct`.
`NOT IN` is also wrong: it returns `NULL` (not `TRUE`) when the subquery yields
a NULL, silently dropping rows.

### 3.3 Cursor pagination is not `WHERE id > x`

The cursor identifies a *row*; the ordering may be on a different column.
Requires `ROW_NUMBER() OVER (<order>)` plus a PK tiebreaker. `take` may be
**negative** — the N rows *preceding* the cursor, obtained by inverting the
order, taking N, then reversing the slice back in Python.

### 3.4 `distinct` is post-fetch, first-seen-in-order dedup

Not `SELECT DISTINCT`, not `DISTINCT ON`. It dedupes on the listed field tuple
keeping the first row in result order, applied **after** `take`/`skip`. So
`take=2, distinct=['status']` can return fewer than 2 rows where `DISTINCT ON
… LIMIT 2` returns exactly 2. Implement in Python — that is what the engine does,
and it makes `include` composition free.

### 3.5 Nested `include` pagination is per-parent

`include={'posts': {'take': 1}}` means one post **per user**, not one total.
Requires `ROW_NUMBER() OVER (PARTITION BY fk ORDER BY …)`, with the PK appended
to the ordering for determinism. The engine issues **one query per relation
level** with `WHERE fk IN (parent_ids)` — it is not N+1, and the replacement
must not be either.

`LATERAL` is a valid PostgreSQL depth-1 optimisation but cannot be the only
implementation (no SQLite support, multiplicative row explosion at depth ≥2).

Nested `cursor` across multiple parents is genuinely under-specified and
version-dependent: **needs a differential oracle, not a spec reading.**

### 3.6 The alias table is applied to user data

`_transform_aliases` walks every dict key at every depth, unconditionally. A
column literally named `order_by`, `not_in`, `is_not`, `startswith`,
`has_some`, or `connect_or_create` is **silently renamed inside `data={}`**.
Reproduce bug-for-bug, or fix deliberately and document the break.

### 3.7 Round-trip encoding

Emit the **engine wire encoding**, not native Python objects. Otherwise the
default pydantic backend breaks while slim/msgspec keep working — a
backend-dependent bug that only surfaces if all three are tested.

| type | wire form | trap |
| --- | --- | --- |
| `DateTime` | ISO-8601, tz-aware UTC | SQLite/MySQL return **naive** — `.tzinfo` becomes `None` |
| `Json` | JSON **string** | SQLAlchemy returns a parsed `dict` from JSONB |
| `Bytes` | base64 **ASCII string** | raw `bytes` is misread as *already-encoded* |
| `BigInt` | string | |
| `Decimal` | string | but raw-query path deliberately returns `float` (lossy) |

Also: microseconds are truncated to milliseconds on the way in
(`serialize_datetime`), which changes equality-filter results. Reproduce it.

And relations that were not included must be **absent/`None`, never `[]`** —
`None` means "not fetched", `[]` means "fetched, empty".

### 3.8 Engine-generated values

`@default(cuid())`, `uuid()`, `nanoid()`, `now()` and `@updatedAt` are generated
by the **engine**, not the database. A pure-Python cuid becomes a hard runtime
dependency. `@updatedAt` must be bumped on *every* update — including rows
touched by a nested `connect`/`disconnect`/`set`.

Silver lining: because every PK in a cuid schema is client-generated,
**pre-generate all PKs in Python** and every `RETURNING` round-trip disappears —
the nested-write plan becomes a freely reorderable, topologically sorted batch.
Schemas using `autoincrement()` still need `RETURNING`.

---

## 4. Nested writes

### 4.1 Relation shapes — the only thing that determines ordering

| shape | FK lives on | ordering |
| --- | --- | --- |
| **S1** to-one, FK on parent | parent row | child INSERT **before** parent |
| **S2** to-one, FK on child (unique) | child row | parent INSERT **before** child |
| **S3** to-many, FK on child | child row | parent INSERT **before** children |
| **S4** implicit m2m join table | join table | both endpoints first |

### 4.2 FK nullability decides legality

`disconnect` and `set` require `UPDATE child SET fk = NULL`. In the 41-model
reference schema only ~10 of ~55 to-many relations have a nullable child FK;
the other ~45 **must raise P2014**. The existing test suite cannot catch this
because its schema declares `author User?` — optional.

### 4.3 Cycles

`Post.currentRevisionId → PostRevision.id` and `PostRevision.postId → Post.id`
form a genuine cycle, and a single call can require **both orderings on the same
pair of tables**. Break by deferring the write on a nullable edge:

```sql
INSERT INTO "Post"         (..., currentRevisionId) VALUES (..., NULL);
INSERT INTO "PostRevision" (..., postId)            VALUES (..., :post_id);
UPDATE "Post" SET "currentRevisionId" = :rev_id WHERE id = :post_id;
```

If no edge in the cycle is nullable the operation is infeasible without
`DEFERRABLE INITIALLY DEFERRED` constraints (which Prisma Migrate does not
emit) — raise a clear, named error.

### 4.4 Do not use the ORM unit-of-work

`Session.add()` + `flush()` sorts INSERTs by **mapper-level** dependency and
handles cycles with a per-relationship `post_update=True`. Prisma's ordering is
**per-query** — the same `Post`/`PostRevision` pair needs opposite orderings in
different calls. Emit `sa.insert()` / `sa.update()` / `sa.delete()` Core
constructs against a `Connection` in a computed order.

### 4.5 Bug-compat items to pin with tests

- **`connect` silently steals**: connecting an already-owned child re-parents it.
- **`update()` / `delete()` swallow `RecordNotFoundError` into `None`** — including
  when it originates from a nested write. Almost certainly unintended upstream,
  but it is the observable contract.
- **Checked vs Unchecked inputs are merged**: `PostOptionalCreateInput` carries
  both `siteId` and `site`. Real Prisma has two disjoint input types and rejects
  supplying both; that discrimination must be reimplemented.
- **`Reaction`'s compound unique has nullable members**, so it is not actually
  unique in PostgreSQL and `connect` can match >1 row. Prisma has the same hole —
  match it (first row in PK order) and pin as a known quirk.

### 4.6 Cascades

Declare `ondelete='CASCADE'` on the FKs and set **`passive_deletes=True` on
every relationship**. Without it SQLAlchemy loads children and issues
`UPDATE child SET fk = NULL`, which fails outright on required FKs and diverges
from the database cascade. One flag; catastrophic if missed.

---

## 5. Errors

Reuse the existing funnel: synthesize the engine's error dict and call
`handle_response_errors`. That preserves load-bearing dispatch order for free —
including a message sniff (`'A value is required but not set'`) that
deliberately wins over the error-code lookup.

| exception | engine | SQLAlchemy / DBAPI |
| --- | --- | --- |
| `UniqueViolationError` | P2002 | PG 23505, MySQL 1062, SQLite `UNIQUE constraint failed` |
| `ForeignKeyViolationError` | P2003 | PG 23503, MySQL 1452/1451 |
| `RecordNotFoundError` | P2025 | `rowcount == 0` on a unique-scoped UPDATE/DELETE; missing `connect` target |
| `RawQueryError` | P2010 | **any** DBAPI failure on the raw path |
| `TableNotFoundError` | P2021 | PG 42P01 — but **only on the model path**; raw goes to `RawQueryError` |
| `InputError` | P2019 | PG class 22xxx |
| *(none — plain `DataError`)* | P2011 | NOT NULL violation. P2011 is **absent** from the mapping table; raising `MissingRequiredValueError` here would be a behaviour change |

`.data` / `.code` / `.meta` are public and introspected by user code
(`exc.meta['target']` is the documented way to branch on which constraint
fired). Those dicts must be synthesized per provider, which requires a
constraint→columns map built from the schema.

---

## 6. Schema and migrations

### 6.1 Split the switch

`modelBackend = "sqlalchemy"` (record classes) and `engineType = "sqlalchemy"`
(query execution) should be **orthogonal**. The schema half ships alone, is
independently valuable and independently verifiable: generated ORM models plus
an Alembic baseline provably matching the live database, while Prisma still
serves queries.

### 6.2 Constraint names: reconcile, do not derive

Prisma's identifier truncation is undocumented and version-dependent.
Reflect the live database, match objects to metadata **by column tuple** (names
are exactly what is untrusted), overwrite, and emit explicit `name=` arguments.
Unmatched in either direction is a hard failure with an actionable diff.

### 6.3 The empty autogenerate diff is necessary but NOT sufficient

Alembic autogenerate is structurally blind to **FK `ondelete`/`onupdate`**,
CHECK constraints, index methods/opclasses, column ordering, and sequence
details. A wrong referential action passes the gate and is data-loss-shaped.

**Gate A** — programmatic `produce_migrations`, empty `upgrade_ops`.
**Gate B** — DDL equivalence: build one scratch DB via `prisma migrate diff`,
another via `metadata.create_all()`, dump both, normalise, require empty diff.
**Gate C** — round-trip: `alembic upgrade head` from empty, then Gate B again,
proving the rendered baseline file (not just the metadata object) is correct.

### 6.4 Migration history handover

`_prisma_migrations` and `alembic_version` are structurally incomparable. Do
**not** synthesize one Alembic revision per Prisma migration — the checksums are
over Prisma's own SQL, the revision graph would be fabricated, and `downgrade`
would be catastrophic. Instead: assert quiescence, assert no drift, snapshot
`_prisma_migrations` to an archive table, write a linkage row, stamp the
baseline, re-run Gate A. And add `_prisma_migrations` to Alembic's
`include_object` exclusions, or the first post-cutover autogenerate proposes
dropping the audit trail.

### 6.5 One model set, two interfaces

`sa/models.py` must be engine-free and **byte-identical between `interface=sync`
and `interface=asyncio`** — assert that in CI. Only `sa/engine.py` is
interface-conditional. Every relationship gets `lazy="raise"`: under async a lazy
load raises `MissingGreenlet`, under sync it silently N+1s; `raise` makes both
fail identically *and* preserves Prisma's semantics, which have no implicit lazy
loading.

---

## 7. Conformance testing

### 7.1 The free gate

`databases/` already exists: ~30 test modules x {async, sync} x 5 providers,
feature-gated, with shared snapshots. Running it with
`PRISMA_PY_ENGINE=sqlalchemy` is a complete behavioural suite for zero new code.

It is **complementary, not redundant**: it asserts *absolute* expected values.
A bug both engines share passes every differential test and only the absolute
suite catches it. That makes it a required gate.

### 7.2 Differential harness — three layers

| layer | what | catches |
| --- | --- | --- |
| **L1** | raw `_execute` envelope, before `model_parse` | type-coercion drift, `None` vs missing key |
| **L2** | `model_dump()` of the result | deserialiser divergence across the three backends |
| **L3** | full dump of all tables after the case | **writes that return the right payload but persist the wrong rows** |

L3 is not optional: a nested `delete_many` escaping the parent scope returns a
perfectly correct payload while wiping unrelated rows.

Write isolation via PostgreSQL template-database cloning (~150–400 ms) gives both
engines byte-identical starting state, including sequence values.

### 7.3 Normalise ordering from the intent, never the payload

> A list is compared as a **multiset** iff the intent node that produced it had
> no `orderBy`, `cursor` or `distinct`. Otherwise strict sequence.

Plus a **stability probe** (run the reference 3x; if its order is stable, an SUT
difference is still reported, just soft) and a **deliberate tie corpus** — rows
with identical sort keys — so every ordering-sensitive shape has both an
"ordered by tied column only" case and an "ordered by `[tied, id]`" case.

Masking of engine-generated values is **referentially consistent**: a token map
assigns `<id:1>` on first sight and reuses it, so a SUT that sets `author_id` to
a *different* generated id yields `<id:2>` and fails. Mask set built from the
DMMF, never by regex.

### 7.4 Property testing needs a value pool

A generator drawing arbitrary strings produces queries that all return `[]`, and
**two engines agree trivially on empty**. Draw ~65% from actual live column
values, ~20% type edge constants, ~15% arbitrary; use Hypothesis `target()` on
row count; gate on **≥95% of examples returning ≥1 row**.

### 7.5 Cheapest high-value artifact

`tests/test_sql_compile.py` — DB-less snapshots of generated SQL. The only place
a reviewer *sees* the SQL, and asserting **statement count** catches N+1
deterministically where wall-clock never will.

CI must run `--inline-snapshot=disable`, or `--inline-snapshot=fix` is a
one-keystroke way to erase exactly the signal these tests exist to produce.

### 7.6 Shadow mode

`PRISMA_PY_ENGINE=shadow` runs both engines at the seam, returns the binary
result, logs diffs. ~30 lines, and the strongest de-risking tool available
because it exercises **the user's own queries** — the ones no corpus contains.

### 7.7 Waivers expire

Every entry in `known_differences.toml` carries an `expires` date; an expired
waiver is a build failure demanding a human decision. Zero T1/T2 (CRUD, filters)
waivers permitted, ever.

---

## 8. Cannot be preserved

1. **`get_metrics()`** — engine-internal counters, no analogue.
2. **`batch_()` pipelining** — atomicity survives, but N statements become N
   round trips instead of one. A silent latency cliff for exactly the workload
   `batch_()` exists to serve.
3. **`PRISMA_PY_RAW_DECODE`** becomes a no-op — there are no bytes to decode.
   Must be gated off or it forces a pointless serialize→parse round trip.
4. **`EngineRequestError`, `UnprocessableEntityError`, `BinaryNotFoundError`,
   `MismatchedVersionsError`** become unreachable. Keep them importable.
5. **Error message prose** — Rust-engine wording is not reproducible. Class and
   code are contract; message is not.
6. **Read snapshot consistency across a multi-statement `include`** — today one
   engine call is consistent; N round trips are not, unless reads are wrapped in
   a transaction. A real, currently-absent hazard requiring an explicit decision.

---

## 9. Staged plan

Each stage independently shippable and revertible; no stage starts until the
prior gate is green **in CI**, not on a laptop.

| stage | ships | gate |
| --- | --- | --- |
| **0** ✅ | DMMF gaps G1, G3, G4, G5-`schemas`; `schemaMetadata` emits the physical schema. `@db.*` closed in 1.1.0 by lexing the raw schema text, `@relation(map:)` in 1.2.0 the same way. **Left open:** `relationMode`, also not on the wire (§2.1) | golden-file test: the set of keys in the raw wire JSON equals the set of fields on each Pydantic model — zero silent drops. Plus absolute assertions on the reconstruction itself (`test_schema_metadata.py`), naming the exact table, column and constraint — a differential test against the query engine passes on any misreading both sides share |
| **1** ✅ | `prisma.sa`: the schema half — `MetaData`/`Table` built from `schemaMetadata`. Prisma still executes queries. **Changed from the plan:** no `modelBackend = "sqlalchemy"` and no `PrismaRecordMixin` (§9.1) | stronger than the planned gate: `prisma db push` and `MetaData.create_all()` into two databases, `pg_dump --schema-only` **identical**. Empty autogenerate diff is necessary but not sufficient — it ignores FK actions, constraint names, index methods, column order and CHECKs |
| **2** | migration CLI: `generate` / `baseline` / `verify` / `handover` / `doctor` | on a DB built by real `prisma migrate deploy`, handover completes and post-handover Gate A is empty; adding one column then produces **exactly** one `op.add_column` |
| **3** | `engineType = "sqlalchemy"` — the compiler. Tier 1 CRUD first, then filters, relations, nested writes, aggregates | differential corpus T1–T6 + `databases/` suite under the new engine |
| **4** | shadow mode, property testing, deep nightly | ≥7 consecutive green nightlies, zero T1/T2 waivers |
| **5** | default flip, then deprecate binary | two releases at stage 4 with no new user-reported difference |

### 9.1 Why Stage 1 dropped `modelBackend = "sqlalchemy"`

The plan had records become SQLAlchemy declarative instances via a
`PrismaRecordMixin`, so that `db.user.find_many()` returned objects usable in a
`Session`. That is incompatible with the standing constraint on this work — **no
changes in signatures nor payloads** — because it changes the type of every
value the client returns.

What Stage 1 ships instead is the schema half only: `MetaData` and `Table`
objects, built at runtime from `metadata.SCHEMA`. Records are untouched.

That is not a lesser deliverable for what comes next. Stage 3 compiles to
SQLAlchemy **Core**, which consumes `Table` objects, not declarative classes;
the ORM layer was never on the path. And it is independently useful today —
Alembic, reflection and hand-written Core queries all work against a
Prisma-managed database without changing a line of Prisma code.

Building the `MetaData` at runtime rather than emitting declarative source from a
Jinja template was the other departure: one implementation tested once against a
real database, instead of template output that has to be re-verified for every
schema shape. `prisma py sqlalchemy generate` (Stage 2) dumps declarative source
for people who want it checked in.

### 9.2 What Stage 1 refuses to do

Each of these is a case where a plausible answer exists and would be silently
wrong, so the code raises or flags instead:

| case | why it cannot be answered | consequence |
| --- | --- | --- |
| self-referential implicit m2m | which side is join column `A` is not in the DMMF | flagged `join_ambiguous`; traversal needs an explicit decision |
| `@db.*` native types | not sent through the generator protocol at all (verified) | precision/length annotations invisible; recoverable only by lexing the schema text |
| `relationMode = "prisma"` | not sent either | the database has *no* FKs; the constraints we build would diff against every table |
| any provider but PostgreSQL | type table not verified against a real `db push` | `UnsupportedProviderError` naming the provider |
| an unmapped `@db.*` annotation | no verified type for it | raises naming the annotation, rather than falling back to the default type |

Three annotations Prisma does not send in the DMMF are recovered by lexing the
raw schema text (`generator/_native_types.py`): `@db.*`, and `@relation(map:)`.
`relationMode` is the remaining one and is recoverable the same way.

---

## 10. Open decision

**Scope of the nested-write surface** (§2.4). Implementing only the typed
subset is a materially smaller job but silently breaks users relying on the
untyped-but-working operations. Implementing the full runtime surface requires
a fully recursive planner from day one.

Recommendation: **full runtime surface for reads; typed subset plus `update`,
`upsert` and `deleteMany` for writes**, with a loud `NotImplementedError` naming
the operation for anything else. A loud refusal is a documented limitation; a
silent difference is a data-corruption bug.
