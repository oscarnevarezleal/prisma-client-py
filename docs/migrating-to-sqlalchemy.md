# Migrating from Prisma Client Python to SQLAlchemy

!!! tip "Running this with an agent?"

    [**The runbook**](prisma-to-sqlalchemy-runbook.md) is the procedural version
    of this page: ordered phases, exact commands, verified translation tables,
    and explicit STOP conditions. Every translation in it was executed against a
    live database and compared. This page explains *why*; the runbook says
    *what to do*.

This is a route map for an application that is already on Prisma Client Python and
wants to end up on SQLAlchemy, written against *this library's* conventions rather
than generic ORM-porting advice. The key insight is that almost every seam you need
already exists in the client, so the migration can be incremental and reversible at
every step — you never need a flag day.

The four seams this path leans on:

| Seam | Where it lives | Why it matters |
| --- | --- | --- |
| The schema is data, not code | `schema.prisma` → DMMF → [custom generators](reference/custom-generators.md) | You can emit SQLAlchemy models from the same source of truth |
| Client access is indirected | [`prisma.register()` / `get_client()`](reference/client.md) (`src/prisma/_registry.py`) | You can swap what call sites resolve to without touching them |
| Models are plain Pydantic | `prisma/models.py`, all subclasses of `_PrismaModel` | They survive as DTOs after the query layer is gone |
| Raw SQL is first-class | `query_raw` / `execute_raw`, `prisma/_raw_query.py` | Both stacks can talk to the same database, in the same transaction shape |

---

## Phase 0 — establish the seam

Before anything moves, make sure your application code does not construct
`Prisma()` directly all over the place. The library already provides the
indirection:

```py
from prisma import Prisma, register

db = Prisma(auto_register=True)   # or register(db) / register(lambda: db)
```

and every call site resolves through `get_client()` — including the model-scoped
form, `User.prisma()`, which calls `get_client()` internally when no client is
passed (see `bases.py.jinja`).

If call sites already go through `get_client()` or through an injected client, you
have exactly one place to change later. If they don't, this is the single highest-value
refactor to do *first*, while the app is still 100% Prisma and every test still passes.

`prisma.testing.reset_client()` is the context manager that lets tests swap the
registered client; it is the same lever you will use to run a test suite against
either backend during the transition.

---

## Phase 1 — take ownership of the schema

Prisma's schema is not just an input to codegen; it also owns migration history in
`prisma/migrations/`. SQLAlchemy + Alembic wants to own that. Decide the boundary
explicitly:

**Recommended:** keep `schema.prisma` as the source of truth for *DDL* until the very
end. `prisma migrate` keeps working, the database keeps evolving, and SQLAlchemy models
are generated *from* the schema (Phase 2) rather than hand-written against it. This
keeps the two stacks structurally incapable of drifting.

**Then, at cutover:** run `alembic revision --autogenerate` once against the final
database. Because the SQLAlchemy models were generated from the same DMMF, that first
revision should be empty — which is the proof that your models match the database.
Stamp it (`alembic stamp head`) and Alembic owns DDL from that point on.

Do not run both migration tools against the same database at the same time. The
`_prisma_migrations` table and `alembic_version` table are independent and neither
knows about the other.

---

## Phase 2 — get the SQLAlchemy schema

### The short version: `prisma.sa`

This fork ships the translation, so for most schemas Phase 2 is two lines of config
and one import.

```prisma
generator client {
  provider       = "prisma-client-py"
  schemaMetadata = true
}
```

```py
from prisma import sa

metadata = sa.metadata()          # a sqlalchemy.MetaData
accounts = sa.table_for('Account')  # a sqlalchemy.Table, resolving @@map
```

`sa.metadata()` describes the same database `prisma db push` creates. That is not a
claim about intent — the test that gates this package builds one database with
`prisma db push` and another with `MetaData.create_all()`, and requires
`pg_dump --schema-only` to come out **identical**: same tables, column order, types
and precisions, constraint names, foreign key actions, indexes, enum labels and
implicit many-to-many join tables.

That is a deliberately stronger bar than an empty Alembic autogenerate diff, which is
what most schema-translation tools are checked against. Autogenerate does not compare
foreign key `ondelete`/`onupdate`, constraint names, index methods, column ordering or
CHECK constraints — so it stays quiet about exactly the differences that hurt later.
Point Alembic's `target_metadata` at `sa.metadata()` and you get the empty first
revision Phase 1 asks for.

Requires PostgreSQL today. The type table is verified against a real database rather
than against documentation, and only providers checked that way are listed; an
unverified provider raises `UnsupportedProviderError` naming itself rather than
emitting a plausible-looking schema that diffs on someone else's deploy.

`@db.*` native types **are** honoured. Prisma does not send them through the
generator protocol — verified, not assumed — so they are recovered by lexing the raw
schema text, which the generator payload does carry. An annotation with no mapping
raises naming itself rather than falling back to the default type: a `@db.Uuid` read
as `text` is not a diff to fix later, it is a full-database rewrite.

Two things it will not guess:

- **A self-referential implicit many-to-many.** The join table is built, but which
  side is column `A` is not recoverable from the DMMF, so the relation is flagged
  `join_ambiguous`. Traversing it needs an explicit decision from you.
- **`relationMode = "prisma"`.** Absent from the generator payload. Under it the
  database has *no* foreign keys at all, so the FK constraints built here would be a
  diff against every table.

### The long version: your own generator

If you want declarative classes checked into your repo, a different naming scheme, or
mixins on the generated models, the DMMF is right there. Prisma sends generators the
schema in AST form, and Prisma Client Python exposes it as Pydantic models you can
subclass a generator against:

```py
from pathlib import Path
from prisma.generator import BaseGenerator, Manifest, DefaultData

class SQLAlchemyGenerator(BaseGenerator):
    def get_manifest(self) -> Manifest:
        return Manifest(
            name='SQLAlchemy models',
            default_output='sa_models.py',
        )

    def generate(self, data: DefaultData) -> None:
        out = Path(data.generator.output.value)
        out.write_text(render(data.dmmf.datamodel))

if __name__ == '__main__':
    SQLAlchemyGenerator.invoke()
```

Register it alongside the existing client generator so both run on every
`prisma generate`:

```prisma
generator client {
  provider = "prisma-client-py"
}

generator sqlalchemy {
  provider = "python sqlalchemy_generator.py"
  output   = "../app/db/models.py"
}
```

Everything you need to emit a faithful SQLAlchemy model is already on the DMMF
`Model` / `Field` objects (`src/prisma/generator/models.py`):

| Prisma / DMMF | SQLAlchemy |
| --- | --- |
| `model.name`, `model.db_name` (`@@map`) | class name, `__tablename__` |
| `field.name`, `field.db_name` (`@map`) | attribute name, `Column(name=...)` |
| `field.type` + `field.kind` | column type (see mapping below) |
| `field.is_id`, `model.compound_primary_key` | `primary_key=True`, `PrimaryKeyConstraint` |
| `field.is_unique`, `model.unique_indexes` | `unique=True`, `UniqueConstraint` |
| `field.is_required` | `nullable=False` |
| `field.is_list` (scalar) | `ARRAY(...)` — Prisma only allows these on PostgreSQL/CockroachDB, and the column is NULLable despite `is_required` |
| `field.has_default_value`, `field.default` | `default=` / `server_default=` |
| `field.is_updated_at` (`@updatedAt`) | `onupdate=func.now()` — Python-side, as Prisma also emits no DDL for it |
| `field.relation_from_fields` / `relation_to_fields` | `ForeignKey(...)` |
| `field.relation_name` | `relationship(back_populates=...)` pairing |
| `field.relation_on_delete` | `ondelete=` on the FK |

Note the direction convention: the side of a relation that holds
`relation_from_fields` is the side that owns the foreign key. That is exactly the
information `relationship()` / `ForeignKey()` need, so the pairing is mechanical.

Five places the mechanical translation is wrong, all of them found by diffing DDL
against a real `prisma db push` rather than by reading the docs:

- **`@default(cuid())` and `@default(uuid())` are client-side.** They produce no
  DDL. A `server_default` there diffs against every real database, and worse, hides a
  missing client-side value behind an INSERT that quietly succeeds.
- **An undeclared `onDelete` is `RESTRICT` for a mandatory relation and `SET NULL`
  for an optional one** — not `CASCADE`, not `NO ACTION`. `onUpdate` is `CASCADE` for
  both, and is not reported in the DMMF at all, so a non-default `onUpdate:` is
  invisible.
- **Prisma emits uniques as `CREATE UNIQUE INDEX`, even on PostgreSQL.** Alembic
  distinguishes a unique index from a `UniqueConstraint`, so the idiomatic choice
  means a drop-and-recreate on every migration.
- **A field-level `@unique` is not in `model.unique_indexes`.** It appears only as
  `field.is_unique`, so reading `unique_indexes` alone silently loses every
  single-column unique in the schema.
- **`DateTime` is `timestamp(3)`, not `timestamp`,** and `Decimal` is
  `numeric(65,30)`. Precision that a translation drops is a column alteration on
  every migration afterwards.

Scalar types map through the same table the client uses (`TYPE_MAPPING` in
`generator/models.py`), with three that need a decision rather than a lookup:

- **`Json`** — the client wraps these in `prisma.fields.Json`. Map to `JSON`/`JSONB`
  and decide whether call sites keep the wrapper. Usually they shouldn't.
- **`Bytes`** — the client exposes `prisma.fields.Base64`, a *Base64-encoded wrapper*,
  not raw bytes. SQLAlchemy `LargeBinary` gives you raw bytes. This is a real
  behavioural difference at every call site that touches a `Bytes` column; grep for
  `Base64` early and count them.
- **`Decimal`** — behind the `enable_experimental_decimal` flag in Prisma Client
  Python, plain `Numeric` in SQLAlchemy. Check precision/scale explicitly.

Emit both the sync and async flavours from one module — SQLAlchemy 2.x uses the same
`DeclarativeBase` classes for `Session` and `AsyncSession`, which is the property the
Prisma client lacks and which [`unified-sync-async-client.md`](contributing/unified-sync-async-client.md)
is about.

---

## Phase 3 — translate the query layer

This is the bulk of the work and it does not benefit from cleverness. The mapping is
mostly one-to-one; the traps are in the semantics, not the syntax.

| Prisma Client Python | SQLAlchemy 2.x |
| --- | --- |
| `db.user.find_unique(where={'id': 1})` | `session.get(User, 1)` |
| `db.user.find_first(where=..., order=...)` | `session.scalars(select(User).where(...).order_by(...)).first()` |
| `db.user.find_many(where=..., take=, skip=)` | `select(User).where(...).limit().offset()` |
| `include={'posts': True}` | `.options(selectinload(User.posts))` |
| `select={'name': True}` | `select(User.name)` |
| `db.user.create(data=...)` | `session.add(User(...))` |
| `db.user.create_many(data=[...])` | `session.execute(insert(User), [...])` |
| `db.user.update(where=..., data=...)` | mutate the instance, or `update(User).where(...)` |
| `db.user.upsert(where=..., data={'create':…, 'update':…})` | dialect `on_conflict_do_update` |
| `db.user.delete_many(where=...)` | `delete(User).where(...)` |
| `db.user.count(where=...)` | `select(func.count()).select_from(User).where(...)` |
| `db.user.group_by(by=[...], count=...)` | `select(...).group_by(...)` |
| `async with db.tx() as tx:` | `async with session.begin():` |
| `async with db.batch_() as batcher:` | one `session.flush()` / `commit()` |
| `db.query_raw(sql, *args, model=User)` | `session.scalars(text(sql), params)` |

Four semantic differences that cause most of the real bugs:

1. **Identity map.** Prisma returns detached Pydantic values; SQLAlchemy returns
   *managed instances* tied to a `Session`. Code that assumed a fetched record was a
   plain immutable value may now be mutating the database on the next `flush()`.
   This is the single biggest behavioural change in the migration.

2. **Lazy loading.** `include=` is explicit and eager in Prisma. In SQLAlchemy,
   an un-loaded relationship attribute will happily emit a query on attribute access —
   or raise `MissingGreenlet` under asyncio. Set `lazy='raise'` on relationships during
   the migration so unmigrated assumptions fail loudly instead of silently N+1-ing.

3. **Nested writes.** Prisma's `data={'posts': {'create': [...]}}` has no direct
   SQLAlchemy equivalent; it becomes cascade configuration plus explicit object graph
   construction. Inventory these before you start — they are the operations most likely
   to need genuine redesign rather than translation.

4. **`None` on required list fields.** Prisma Client Python normalises `None` to `[]`
   for required array fields returned from raw queries (see
   `_transform_required_list_fields` in `models.py.jinja`). SQLAlchemy does not. If you
   rely on that normalisation, port it as a validator.

`batch_()` deserves a specific note: it is a *pipelining* construct, not a
transaction-isolation one. Its closest SQLAlchemy analogue is accumulating pending
objects and flushing once — not `session.begin()`, which you should map from `tx()`.

---

## Phase 4 — run both, cut over per module

Because the models are generated from one schema and both stacks speak SQL to the same
database, you can migrate one module at a time. The mechanics:

- Point SQLAlchemy at the same `DATABASE_URL` the Prisma datasource resolves
  (`OptionalValueFromEnvVar.resolve()` is what the client itself uses).
- Migrate read paths before write paths. Reads are verifiable by comparison; writes
  are not.
- For a module under migration, run the Prisma query and the SQLAlchemy query and
  assert equality in tests. The Pydantic models are the natural comparison currency —
  `SAUser` → `PrismaUser.model_validate(...)` gives you a structural diff for free.
- Do not interleave writes from both stacks inside one logical transaction. They hold
  separate connections; the Prisma query engine is a separate process and will not join
  a SQLAlchemy transaction.

Keep the generated Prisma Pydantic models as your API/DTO layer even after the query
layer is gone if they are already in your response schemas. They are ordinary
`pydantic.BaseModel` subclasses with no engine dependency — decoupling them from
Prisma is a rename, not a rewrite. This also lets you delete the client dependency
before you finish rewriting your serialisation layer.

---

## Phase 5 — remove Prisma

Only once no module resolves `get_client()`:

1. Drop the `generator client` block, keep the `generator sqlalchemy` block (or retire
   `schema.prisma` entirely and let Alembic autogenerate own the schema).
2. Autogenerate the Alembic baseline and confirm it's empty (Phase 1).
3. Remove the `prisma` dependency. This also removes the ~10–20 MB Rust query engine
   binary and the Node CLI bootstrap from your image, and the per-process client import
   cost documented in [`benchmarks/`](https://github.com/oscarnevarezleal/prisma-client-py/tree/develop/benchmarks).
4. Delete `prisma/migrations/` and `_prisma_migrations` from the database *last*, after
   a release has gone out clean — it is your only rollback record.

---

## What it costs you to stay (measured)

If performance is part of why you're considering this migration, the numbers
are worth having before you decide. Measured on this repo's
[`benchmarks/pg-lab/`](https://github.com/oscarnevarezleal/prisma-client-py/tree/develop/benchmarks/pg-lab)
rig (41-model schema, local PostgreSQL 16, warm connections):

| | 1 row | 400 rows |
| --- | ---: | ---: |
| postgres itself (psycopg) | 0.17 ms | 0.90 ms |
| the same query through Prisma | 1.96 ms | 7.56 ms |
| **query-engine overhead** | **91%** | **88%** |

The database is ~10% of a Prisma query; the binary query engine — a subprocess
hop plus GraphQL parse/plan/serialize per call — is the rest. This is not a
tuning problem: connection pooling, HTTP keep-alive and concurrency scaling
were all measured and are all working correctly.

Two honest implications:

- **A driver-level stack removes the ~90%, rather than optimizing the ~10%.**
  If per-query latency is your binding constraint, that is the strongest
  argument for this migration, and no amount of client-side work substitutes
  for it.
- **If it isn't your constraint, this is a weak reason to migrate.** 2 ms per
  query is irrelevant to most request paths, and you would be trading away the
  schema-as-source-of-truth workflow, typed query arguments, and Prisma's
  migration tooling to recover it. Before committing, check whether your
  latency actually lives in queries — and note that `query_raw` already
  reclaims 28-46% of it by skipping GraphQL planning, while keeping typed
  records.

## What you gain and lose

**Gain:** one model layer for sync and async (SQLAlchemy 2.x shares `DeclarativeBase`
across `Session` and `AsyncSession`); no separate query-engine process or binary;
arbitrary SQL without dropping to `query_raw`; Alembic's migration ecosystem; and the
memory profile that motivates this fork's generator options in the first place.

**Lose:** Prisma's type-level query safety (SQLAlchemy 2.x typing is good but does not
model `WhereInput` shapes), nested writes, the DMMF as a machine-readable schema, and
cross-language schema sharing if a TypeScript service reads the same `schema.prisma`.

The last one is worth checking before you start. If another service generates from the
same schema, Phase 1's "keep `schema.prisma` as the source of truth" stops being a
transitional convenience and becomes the permanent arrangement — which is fine, and
makes the Phase 2 generator a long-lived piece of infrastructure rather than a
migration tool.
