# Moving off Prisma Client Python onto SQLAlchemy

This page is for the person who has to decide *what shape* of migration to run
and *in what order* — a tech lead sizing the work, not an engineer executing it.
It assumes you are competent and have never used SQLAlchemy.

!!! tip "Two other documents, and which one you want"

    [**The runbook**](prisma-to-sqlalchemy-runbook.md) is the procedure: ordered
    phases, exact commands, §4.1 of verified translations (every one executed
    against a live PostgreSQL database and compared row for row) and §5, the STOP
    list. It is written for an agent doing the work. Nothing on this page repeats
    it — where a translation matters here, it links there.

    [**Performance findings**](performance-findings.md) is where the numbers on
    this page come from.

    This page is the one you read *before* either of them.

There are four decisions, and they are close to independent. Take them in this
order, because each one narrows the next.

1. Are you replacing the **engine** or the **whole client**?
2. Are you running the two stacks **side by side**, or **cutting over**?
3. Do you want **idiomatic ORM models**, or a **transparent layer** over the
   tables you already have?
4. **Phased** or **big bang**?

---

## First: is this worth doing at all?

Get the honest numbers in front of the room before the room starts arguing about
approach. Everything below is measured on this repo's `benchmarks/pg-lab/` rig —
a 41-model schema, local PostgreSQL 16, warm connections, interleaved runs — and
is reproducible from there.

The same four queries, Prisma client versus SQLAlchemy Core over tables built
from the same schema, both sides asserted to return identical rows before timing:

| query | Prisma | SQLAlchemy Core | saved |
| --- | ---: | ---: | ---: |
| `find_many` + 2 includes, 25 rows | 4.07 ms | 2.22 ms | 45% |
| `find_unique` | 1.84 ms | 0.45 ms | 76% |
| `count` with a filter | 1.43 ms | 0.44 ms | 69% |
| `query_raw`, identical SQL both sides | 1.15 ms | 0.38 ms | 67% |
| **total** | **8.49 ms** | **3.48 ms** | **59%** |

Connecting is the larger ratio, because Prisma has to spawn the query-engine
subprocess and wait for it to become ready: **54–72 ms drops to 6–8 ms.** If you
run short-lived processes — Lambda, a CLI, a worker that reconnects — that is
the number that matters, not the per-query one.

Three things that argument does *not* support, and which you should say out loud
so nobody sells the migration on them:

**Memory is roughly a wash.** Dropping the engine removes a 24.7 MB subprocess;
importing SQLAlchemy costs +26.1 MB and +157 ms of import time. For a single
client that is neutral. It only becomes a saving in a dual sync+async process,
where SQLAlchemy is imported once and Prisma would otherwise pay for two engine
subprocesses. The query-engine *binary* on disk is 18.3 MB for the pinned CLI
5.19.0 on `debian-openssl-3.0.x`, which is an image-size argument, not a runtime
one.

**The absolute saving is small.** The whole 5.01 ms in that table is the total
across four queries — 1.4 ms on a `find_unique`, 1.85 ms on an include. The
database is ~10% of a Prisma query and the engine is the other ~90%, so the
*ceiling* on this migration is real and large — but a ceiling is not a benefit,
and single-digit milliseconds are irrelevant to most request paths. Check where
your latency actually lives first. If it is in the queries, note that `query_raw`
already reclaims 28–46% of the engine overhead today, with typed records, at the
cost of a few hours rather than a quarter.

**Includes recover the least.** A to-many include is where the engine does real
work, and it is also the shape with no mechanical translation — 45% is the
smallest number in the table and the largest amount of hand work.

What you give up is not small: schema-as-source-of-truth, typed query arguments,
Prisma's migration tooling, and — if a TypeScript service generates from the same
`schema.prisma` — cross-language schema sharing. Check that last one before you
start. If it is true, the schema stays wherever it is, permanently, and decision 3
below is already made for you.

---

## Measure your own codebase before deciding anything

Everything after this point is easier once you have run the doctor. It reads your
source, classifies every Prisma call site against the runbook, and changes
nothing:

```bash
prisma py sqlalchemy doctor [--json] [--schema PATH] [--path SRC...]
```

Real output, run against this repo's `examples/` directory (trimmed — the full
run also lists every call site individually):

```
prisma py sqlalchemy doctor
runbook: docs/prisma-to-sqlalchemy-runbook.md — §4.1 is the verified list, §5 is the STOP list
prisma.sa 1.3.0

Scope check (§0)
  provider is PostgreSQL           pass     postgresql
  relationMode is not "prisma"     pass     absent
  @db.* annotations                         Decimal x1, SmallInt x1, Timestamptz x1, Uuid x9, VarChar x1

Schema (build_metadata)
  10 models, 12 tables, 1 enums, 2 implicit m2m join tables (postgresql)
  relation shapes: many-to-many 4, to-many 5, to-one-inverse 1, to-one-owner 6
  no verified translation path:
    - Label.related is a self-referential implicit many-to-many; which side is join
      column `A` is not recoverable from the DMMF, so traversing it would be a coin
      flip [§5 Self-referential implicit m2m]

Call sites
  17 call sites in 6 files scanned: 10 verified, 3 stop, 4 unknown
```

The `Schema` block only appears when the client was generated with
`schemaMetadata = true`. Without it the doctor still scans the source and still
produces every section below, but reports:

```
Schema (build_metadata)
  unavailable: the generated client carries no physical schema metadata; add
  `schemaMetadata = true` to the generator block and re-run `prisma generate` (runbook §2)
```

Three buckets, and the third one is the point. `verified` means a §4.1 row exists
and was executed against a live database. `stop` means a §5 row, named. `unknown`
means **neither** — said out loud rather than rounded into either side:

```
  STOP — §5, no verified translation (3)
    examples/discord-message-counter/bot.py:31  self.prisma.channel.upsert  [§5 Atomic updates]
      `data['update']` sets `total` with an atomic operation (increment), which is
      not verified — `values_for_update` refuses it by name rather than writing the
      mapping into the column
    examples/fastapi-basic/main.py:120  Post.prisma().create  [§5 connect / disconnect / set / connectOrCreate]
      `data` uses connect on `author`; `disconnect` and `set` are illegal against a
      NOT NULL foreign key and none of them is verified

  Unknown — neither §4.1 nor §5 covers it (4)
    examples/fastapi-basic/main.py:60  User.prisma().update  [none — neither §4.1 nor §5]
      `data` is `data`, not a literal, so a nested write or an atomic operation
      inside it cannot be ruled out
    examples/fastapi-basic/main.py:73  User.prisma().delete  [none — neither §4.1 nor §5]
      no §4.1 row covers `include=` on `delete`
```

And it tells you what it could not see, which is the part to read carefully
before you use its numbers for planning:

```
What this report could not analyse
  2 x argument-not-covered: call sites using an argument no §4.1 row covers, e.g. `cursor=`
  1 x opaque-data: call sites whose write payload is built elsewhere — a nested write
      or an atomic operation inside it would be a STOP, and the scanner cannot see one
  1 x unresolved-client: call sites whose receiver could not be traced to a `Prisma`
      client, so the model attribute may not be a model at all
  the scanner reads one module at a time: a client, a `where` or a write payload that
  arrives from another module is not followed, and a call site reached only through a
  helper is attributed to the helper, not to its callers
```

A codebase that builds its `where` clauses in a shared helper will report a lot
of `unknown`. That is not a bug in the tool; it is a real statement that nobody —
including you — can classify those call sites without reading them.

**Calibration.** One team that ran this exercise on a real application measured
**62% of call sites on the STOP list**, because writes and transactions are the
entire mutation surface. Expect your first number to look bad. It should.

---

## Decision 1 — engine replacement, or whole replacement?

Two genuinely different projects share the name "migrate to SQLAlchemy".

**Engine replacement** keeps the call sites. `db.user.find_many(where=...)` stays
in the source; something behind it compiles that to SQLAlchemy Core and executes
it on a SQLAlchemy connection. A team of twenty keeps shipping features with the
codebase knowledge it already has, and the migration is a change to one layer
nobody outside the platform team touches.

**Whole replacement** moves the call sites. `sa.select(user).where(...)` appears
in the source, records stop being Pydantic models, and every engineer who writes
queries learns SQLAlchemy.

What each one touches:

| | engine replacement | whole replacement |
| --- | --- | --- |
| Query call sites | untouched | every one the doctor listed |
| Client construction / `register()` / `get_client()` | replaced — this is the whole change | replaced |
| Types flowing out of the data layer | unchanged (Pydantic models) | change: `Row`/`RowMapping` or ORM instances |
| Serialization, API schemas, DTOs | untouched | touched wherever a record was passed through |
| `except prisma.errors.*` handlers | untouched | rewritten to `sqlalchemy.exc.*` |
| Test fixtures, factories, seeds | mostly untouched | rewritten |
| Transaction boundaries (`tx()` blocks) | untouched | moved to `engine.begin()` |
| Who has to learn SQLAlchemy | the platform team | everyone |

Now the honest part. **The engine-replacement path is not something you can turn
on.** `engineType = "sqlalchemy"` — a compiler that redirects Prisma calls onto
SQLAlchemy Core — is Stage 3 in
[`docs/sqlalchemy-refactor/00-analysis-synthesis.md`](sqlalchemy-refactor/00-analysis-synthesis.md)
§9 and does not exist. What ships today is Stage 1 (the schema, `prisma.sa`) and
Stage 2 (the CLI on this page).

So choosing engine replacement today means *you* write the adapter, in your own
codebase, over the pieces that do exist: `prisma.sa` for the tables and
`values_for_create` / `values_for_update` for the values Prisma fills in on the
client. That is a smaller job than it sounds, because the surface it has to cover
is bounded and enumerated — §4.1 is about a dozen shapes — and because the doctor
tells you exactly which of them your code actually uses. It is still a job that
produces a piece of infrastructure you now own.

The reason to consider it anyway: it is the only path where the number of
engineers who have to change how they work is *one team* rather than *everyone*,
and it converts the migration from a coordination problem into an engineering
problem. Whole replacement is where you end up eventually — the adapter is a
staging post, not a destination — but "eventually" can be two years away and
still be the right plan.

A useful middle: build the adapter for the shapes the doctor calls `verified`,
have it raise loudly on anything else, and let the STOP call sites keep using the
Prisma client directly. That is decision 2.

---

## Decision 2 — coexistence, or cutover?

Coexistence means both stacks are live: new features go on the SQLAlchemy path,
existing ones keep working through Prisma, and modules move across one at a time.
Cutover means one release where everything changes.

Coexistence is almost always right, and it is not free. Be specific about the
bill:

**Two connection pools.** The Prisma query engine is a separate process and pools
its own connections; `sa.create_engine()` holds a `QueuePool` of its own, in the
Python process. Neither knows the other exists, so the database sees the sum.
Size both against `max_connections` before you deploy them together, not after —
this is the coexistence cost that shows up as a production incident rather than a
slow test suite.

**Two transaction scopes that cannot see each other.** This one is measured, not
merely unverified. Inside a single `with db.tx()` block containing one Prisma
write and one SQLAlchemy write:

| | |
| --- | --- |
| the two transaction ids | different |
| the SQLAlchemy half sees the Prisma half | no |
| the Prisma half sees the SQLAlchemy half | no |
| rolling back the SQLAlchemy half | leaves the Prisma half committed |

There is no configuration that fixes this. The Prisma client is a separate OS
process on its own connection; it cannot join a SQLAlchemy transaction, ever.

**Therefore: a `tx()` block migrates whole, or not at all.** No other rule shapes
a coexistence plan as much, because it makes the unit of migration the
transaction rather than the call site. A `tx()` block with five writes in it, one
of which is an atomic increment, stays on Prisma entirely until that increment
has an answer.

The doctor enforces the rule for you. Run against a module with two `tx()` blocks,
one containing an atomic `decrement`:

```
Transactions — §4.1 requires a block to migrate whole
  billing/charges.py:7  tx()  3 call sites — BLOCKED, the whole block stays on Prisma
  billing/charges.py:16  tx()  3 call sites — migratable whole
```

and it propagates the blockage to every call site inside the block, so the
`stop` count you plan against already accounts for it:

```
  STOP — §5, no verified translation
    billing/charges.py:12  tx.entry.create  [§5 A transaction spanning a Prisma client
        and a SQLAlchemy connection]
      inside `tx@billing/charges.py:7`, which contains a call site with no verified
      translation; a transaction cannot span a Prisma client and a SQLAlchemy
      connection, so the block migrates whole or stays on Prisma whole
```

**Both stacks talk to the same database and the same tables.** That part is fine
and is the reason coexistence works at all — point SQLAlchemy at the same
`DATABASE_URL` the Prisma datasource resolves. What is not fine is a read in one
stack expecting to see an uncommitted write from the other.

**Both dependency sets in the image, and both memory costs, for the duration.**
The wash described above is Prisma-alone against SQLAlchemy-alone. During
coexistence you pay both: the 24.7 MB engine subprocess *and* the +26.1 MB
SQLAlchemy import. Memory goes up for the length of the migration and returns to
roughly neutral only when Prisma leaves. Budget it; do not promise it as a saving
mid-flight.

Cutover is the right choice in exactly one situation: the doctor reports few
enough call sites, and few enough STOPs, that the whole thing is one reviewable
pull request. If that is your report, take it — coexistence has real carrying
costs and no benefit when the migration fits in a week.

---

## Decision 3 — idiomatic models, or a transparent layer?

Two ways to have SQLAlchemy know about your schema, and they are not a matter of
taste.

### `prisma py sqlalchemy generate` — declarative classes you check in

```bash
prisma py sqlalchemy generate [--out PATH] [--strict]
```

Emits ordinary SQLAlchemy 2.x declarative source: `class Account(Base)`,
`Mapped[...]` annotations, `mapped_column()`, `relationship()`. Real output from
the reference schema:

```python
class Account(Base):
    __tablename__ = 'accounts'

    id: Mapped[str] = mapped_column(sa.Text(), primary_key=True, nullable=False)
    email: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    slug: Mapped[str] = mapped_column('url_slug', sa.Text(), nullable=False)
    role: Mapped[str] = mapped_column(_role_enum, nullable=False, server_default=sa.text('\'VIEWER\'::"Role"'))
    balance: Mapped[decimal.Decimal] = mapped_column(sa.Numeric(precision=65, scale=30), nullable=False)
    payload: Mapped[Optional[Any]] = mapped_column(postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    createdAt: Mapped[datetime.datetime] = mapped_column(
        postgresql.TIMESTAMP(precision=3),
        nullable=False,
        server_default=sa.text('CURRENT_TIMESTAMP'),
    )
    updatedAt: Mapped[datetime.datetime] = mapped_column(postgresql.TIMESTAMP(precision=3), nullable=False)

    posts: Mapped[List[Entry]] = relationship('Entry', foreign_keys='[Entry.accountId]', back_populates='account')
```

Note the third line: the attribute is the **Prisma field name** (`slug`), the
column is the **mapped name** (`url_slug`). Call sites read the same after the
migration as before, and the DDL is unchanged.

What it refuses to emit, on stderr, rather than guessing:

```
not emitted: Label.related (many-to-many): which of the join table's two columns
(A or B) this side traverses is not recoverable from the DMMF --- the shape is a
self-referential implicit many-to-many. `relationship()` needs explicit
primaryjoin/secondaryjoin conditions to say which, and guessing reverses the
direction of the relation, which silently returns the wrong rows
```

`--strict` turns that into a non-zero exit and writes no file, which is what you
want in CI. Without it you get a compilable module with a comment where the
attribute would have been, so the absence fails loudly at the call site instead
of quietly returning the wrong rows.

The reason this is safe as a second representation of the schema: every
DDL-affecting decision is read off the same `MetaData` that `prisma.sa.metadata()`
builds, never recomputed, and the test suite executes the emitted source and
compares its `Base.metadata` to the built one table by table as compiled DDL.

### `prisma.sa.metadata()` — the same tables at runtime, no file

```python
from prisma import sa

md = sa.metadata()                  # a sqlalchemy.MetaData
accounts = sa.table_for('Account')  # a sqlalchemy.Table — takes the MODEL name; @@map resolved
```

Same schema, built in memory from the metadata the generator emitted, with
nothing to check in and nothing to keep in sync.

**There is deliberately no codegen for this path**, and the reason is worth
stating because it looks like an omission. A generated module that only forwarded
to `prisma.sa.metadata()` would be a file you have to regenerate whenever the
schema changes, in exchange for nothing — the runtime call already returns the
current schema. Codegen earns its keep when the output is something you edit,
review, or add to; a forwarding shim is none of those.

### Choosing

| | generated declarative | `sa.metadata()` |
| --- | --- | --- |
| A file in your repo | yes — reviewed in a PR, greppable | no |
| Kept in sync with `schema.prisma` | you regenerate, or you take ownership — **not both** | automatically |
| Mixins, custom base, renamed attributes | yes, once you own the file | no |
| Query style | ORM (`Session`, `select(Account)`) or Core | Core (`sa.select(accounts)`) |
| What §4.1 is written against | — | this |
| Self-referential implicit m2m | attribute refused | table is there, relation flagged `join_ambiguous` |

The runbook's verified translations are Core, against `Table` objects. If you are
following it mechanically, `sa.metadata()` is the shorter road. Generate
declarative when the team is going to write queries by hand for years and wants
`Account.email` to autocomplete — but understand that you are choosing to own a
file, and "regenerate it" and "hand-edit it" are mutually exclusive.

**One trap that applies to both, and surprises everyone.** Neither path fills in
the values Prisma generates on the client. `@default(cuid())`, `@default(uuid())`,
`@default(nanoid())`, `@default(now())` and `@updatedAt` produce no DDL at all —
so a `sa.insert()` built from these models lands a row with a NULL primary key
where Prisma landed a valid one, and an `@updatedAt` column has no server default
whatsoever, so the INSERT fails on NOT NULL. That is what `values_for_create` and
`values_for_update` are for. They are not optional.

---

## Decision 4 — phased, or big bang?

The doctor's phase grouping *is* the phase plan. It is not generic advice about
strangler figs; it is a partition of your modules by how much of each one has a
verified translation:

```
Phases — what a first slice would cost
  Migratable now — every call site is verified (1 modules)
    examples/kivy-basic/app.py  2 call sites (2 verified, 0 stop, 0 unknown)
  One blocker away — a single STOP in an otherwise verified module (1 modules)
    examples/flask-url-shortener/app.py  3 call sites (2 verified, 1 stop, 0 unknown)
  Mixed — verified work alongside blockers or unanswered questions (2 modules)
    examples/discord-message-counter/bot.py  2 call sites (0 verified, 1 stop, 1 unknown)
    examples/fastapi-basic/main.py  10 call sites (6 verified, 1 stop, 3 unknown)
  Blocked — dominated by call sites with no verified translation (0 modules)

  By model
    Customer  2 call sites (2 verified, 0 stop, 0 unknown) — migratable-now
    Url  3 call sites (2 verified, 1 stop, 0 unknown) — one-blocker
    User  6 call sites (4 verified, 0 stop, 2 unknown) — mixed
```

Read it as four pieces of work with different shapes:

**`migratable-now`** is your first sprint. Every call site has a verified
translation; the work is mechanical and the risk is close to zero. If this bucket
is empty, you do not have a phased migration — you have a research project, and
you should say so before anyone commits a date.

**`one-blocker`** is the highest-value bucket and the one to plan around. A single
STOP holds an otherwise-clean module hostage, and the doctor names which §5 row it
is. Resolving one blocker unlocks a whole module, so this is where a week of
design work buys the most migrated code. Sort it by the blocker, not by the
module: three modules blocked on the same atomic `increment` are one decision,
not three.

**`mixed`** is where estimates go wrong, because `unknown` is in it. An `unknown`
is not "probably fine" — it is a call site nobody has classified, and the reason
is usually that the payload is built somewhere the scanner cannot follow. Budget
reading time, not translation time. Resolve the unknowns first and the module
usually redistributes into the other three buckets.

**`blocked`** modules stay on Prisma. Plan for that explicitly, in the
architecture, rather than as an embarrassment to fix later — they are why
decision 2 matters, and a coexistence plan that has no permanent-Prisma modules
in it is a plan that has not been checked against the report.

The `By model` grouping is the other axis, and it is often the better one. A model
that is `migratable-now` across every module that touches it can move as a unit,
which keeps its reads and writes on the same stack. Migrating a module while
leaving another module writing to the same table through Prisma is legal — both
stacks speak SQL to the same database — but it means that table's invariants are
now enforced in two places.

**Big bang is right when `migratable-now` plus `one-blocker` is almost everything
and the total is small.** Otherwise phase it, and let the report, not the calendar,
decide what goes in each phase.

### The schema handover happens once, and early

Independently of how the queries move, DDL ownership transfers exactly once.
`prisma py sqlalchemy baseline` emits an Alembic project that adopts the database
Prisma already built, rather than trying to recreate it:

```bash
prisma py sqlalchemy baseline [--out DIR] [--models-import MODULE]
```

```
wrote alembic.ini
wrote migrations/env.py
wrote migrations/script.py.mako
wrote migrations/versions/1200480ee486_baseline.py

baseline revision 1200480ee486 covers 12 tables
run `alembic upgrade head` to stamp the database it describes
```

The baseline revision creates nothing — but it is deliberately not a bare `pass`.
Its `upgrade()` asserts the tables are already there and raises if they are not,
because the failure worth guarding against is stamping an *empty* database as
up to date and having every later revision run against a schema that was never
built. `--models-import myapp.models` points `env.py` at your generated models;
omitted, it points at `prisma.sa.metadata()`.

Two rules around this, both from the runbook's §3, which has the full procedure:

- Do not run `prisma migrate` and Alembic against the same database at the same
  time. `_prisma_migrations` and `alembic_version` are independent tables and
  neither knows the other exists.
- Keep `schema.prisma` as the source of truth for DDL until you are ready to hand
  over, then hand over once. Generating the SQLAlchemy schema *from* the Prisma
  schema is what makes the two structurally incapable of drifting in the interim.

**About the equivalence claim, honestly.** `prisma.sa` is gated by a test that
builds one database with `prisma db push` and another with `MetaData.create_all()`
and requires `pg_dump --schema-only` to come out identical — which is a stronger
bar than an empty Alembic autogenerate diff, since autogenerate ignores foreign
key actions, constraint names, index methods, column order and CHECK constraints.
But that is a claim about *the schemas that have been tested*. When a team ran it
against a real 182-model application schema, the dumps were not identical: 2,191
lines of diff, fully explained by two defects that are now fixed, plus three more
found on the retest. The reference schema has grown to cover ten shapes across
three rounds of this. Run the DDL comparison against **your** schema before you trust the
equivalence — the runbook §3 has the command — and treat a non-empty diff as a
finding to report, not as a mistake you made.

---

## The things that do not migrate, and what to do about each

These are on [§5 of the runbook](prisma-to-sqlalchemy-runbook.md#5-stop-list-no-verified-translation).
Present them to the team as **decisions you have to make**, not as gaps that will
close on their own. Nothing here is scheduled.

**Nested writes** — `data={'posts': {'create': [...]}}`. Requires a recursive
planner: ordering, foreign-key satisfaction and rollback across a graph are all
unsolved. `values_for_create` raises `LookupError` naming the relation field
rather than half-translating one. Your decision: rewrite each one as an explicit
sequence of inserts inside a transaction (usually a dozen lines and clearer than
what it replaces), or leave that call site on Prisma. There is no third option.

**`connect` / `disconnect` / `set` / `connectOrCreate`** — none verified, and two
of them are not merely unimplemented: `disconnect` and `set` are *illegal* against
a NOT NULL foreign key, where Prisma itself raises P2014. Your decision: for
`connect`, whether writing the foreign key column directly is equivalent for your
data (it usually is, and it is usually a one-line change); for the rest, whether
the call site should exist at all.

**Atomic operations** — `increment`, `decrement`, `multiply`, `divide`.
`values_for_update` refuses a mapping outright rather than storing the mapping
itself in the column, and the doctor names the operation it found. These are the
ordinary counters and balances of an application — two of the three STOPs in the
`examples/` run above are atomic operations — which is why they are a common
reason for a module to land in `one-blocker`. Your decision:
`sa.update(t).values(clicks=t.c.clicks + 1)` is the obvious SQL and it is *not on
the verified list*, so if you write it you own verifying it against Prisma's
behaviour, including under concurrency. That is a legitimate choice; making it
silently is not.

**`tx(timeout=...)`** — no equivalent exists. Prisma's query engine enforces it
between queries with its own timer; it is neither `statement_timeout` nor
`lock_timeout`, and a `tx()` blocked on a lock outlives it. Your decision: what
that timeout was actually protecting against, and which PostgreSQL mechanism
covers it. Plain `db.tx()` and `db.batch_()` *are* verified — it is only the
timeout argument that has nothing to translate to.

**A transaction spanning both stacks** — impossible, measured, covered above.

**Also on the list:** `aggregate()`, `group_by()`, `Json` field filtering
(`path`, `string_contains`), scalar list filters (`has`, `hasEvery`, `hasSome`),
full-text `search`, `upsert` outside the flat form, self-referential implicit
many-to-many, and any isolation level other than the default. "Not verified"
means exactly that — not "impossible", and not "there is a working translation we
forgot to write down". You can write one; you are then the one who has verified
it.

---

## Traps that were measured, and cost real time

Every one of these is silent. They produce plausible results, pass an
uncontended test, and are discovered in production. They are the reason to follow
the runbook rather than reason from first principles.

**`sa.insert(t).values([...])` drops keys.** SQLAlchemy compiles the `VALUES`
clause from the **first** mapping, so a key only later rows carry is silently
dropped — no error, and the right row count. Two rows, the second alone setting
`role`:

| | row 2's `role` |
| --- | --- |
| Prisma | `ADMIN` |
| one INSERT per distinct key set | `ADMIN` |
| a single `.values([...])` | `VIEWER`, silently |

`conn.execute(sa.insert(t), [...])` behaves the same way. Group the batch by key
set and emit one statement per group.

**`rowcount` is `-1` for INSERT under psycopg**, so it cannot be the source of a
count. Take counts from `.returning(*table.primary_key.columns)` and `len(...)`
instead — which is also what makes `skip_duplicates` countable, since `RETURNING`
on `ON CONFLICT DO NOTHING` yields only the rows actually inserted. `rowcount`
*is* correct for UPDATE and DELETE.

**A `@map`ped enum in a `where` needs the stored label, not the schema member
name.** Prisma addresses a member by its schema name (`ADMIN`); the column only
ever holds the mapped label (`administrator`), and binding `'ADMIN'` is rejected
by the enum type outright. `values_for_create` / `values_for_update` handle the
`data` side; the `where` side is yours:

```python
from prisma._schema import enum_label
accounts.c.role == enum_label('Role', 'ADMIN')   # -> 'administrator'
```

**A write's return value is the same row, but not the same Python objects.**
Prisma parses the engine's response; SQLAlchemy decodes the column type:

| column | Prisma's return value | a `RETURNING` row |
| --- | --- | --- |
| enum | the generated member, `Role.ADMIN` | the stored label, `'administrator'` |
| `DateTime` | aware, UTC | naive, for `timestamp without time zone` |

Under `@map` the two enum strings genuinely differ, so a comparison that used to
match stops matching. Timestamps need
`value.astimezone(timezone.utc).replace(tzinfo=None)` before they compare equal.
The row in the database is identical either way — this is a call-site problem,
not a data problem.

**`upsert` only compiles to a single `INSERT … ON CONFLICT` when `create` gives
the `where` fields the `where` values.** This is not a style rule. Measured from
the engine's own query log, Prisma compiles `upsert` two different ways: one
statement when the precondition holds, and `BEGIN; SELECT; UPDATE-or-INSERT;
SELECT; COMMIT` when it does not, or when `update` is `{}`, or `include=` is
passed, or either payload nests. `ON CONFLICT` keys on the row being *inserted*;
Prisma's `where` selects the row independently, and once they disagree the two
address different rows:

| with a row at `slug='left'` and `create` naming `slug='right'` | rows afterwards |
| --- | --- |
| Prisma | `[('left', 'updated')]` — it round-tripped and updated the existing row |
| `on_conflict_do_update` | `[('left', 'seed left'), ('right', 'created')]` — it conflicted with nothing |

And the read-then-write form is not a safe fallback either: it lands the same
rows as `ON CONFLICT` on every uncontended run, and raises `IntegrityError` under
a concurrent insert of the same key where Prisma updates the other writer's row.
Nothing about testing it will tell you it is wrong.

**One `moment` per batch.** Prisma's `create_many` gives every row in the batch
the same instant and each row its own id. Calling `values_for_create` per row
without passing `moment=` reads the clock once per row — invisible in any
single-row test, visible as rows of one batch disagreeing about `createdAt`.

**`.first()`, not `.one()`.** Prisma's `update` and `delete` return `None` when
nothing matches, and a call site may be relying on that. `.mappings().one()`
raises where Prisma returned `None`.

---

## The library surface

Everything here is in `prisma.sa`, which needs `prisma[sqlalchemy]` installed
(SQLAlchemy 2.0 or later), `schemaMetadata = true` on the generator block, and
PostgreSQL. `sa.__version__` is the contract version of this package, independent
of the client version — it is also the only way to tell this fork from upstream,
which reports the same `prisma.__version__`. Importing `prisma` does not import
SQLAlchemy; `prisma.sa` defers it until something actually needs it.

| | what it gives you |
| --- | --- |
| `sa.metadata()` | the `MetaData` for the whole schema, built once and cached |
| `sa.table_for('Account')` | the `Table` for a **model** name; `@@map` resolved. Passing a table name raises `LookupError` |
| `sa.join_table_for('Post', 'tags')` | the hidden join table behind an implicit many-to-many — it has no model, so `table_for` cannot reach it |
| `sa.values_for_create('Post', data)` | Prisma field names → column names, with `cuid()`/`uuid()`/`nanoid()`/`now()`/`@updatedAt` filled in as the client would. Raises `LookupError` on a relation field |
| `sa.values_for_update('Post', data)` | the same for an update, plus a fresh `@updatedAt`. Refuses atomic operations by name |
| `sa.build_declarative(...)` | what `generate` calls: declarative source plus a list of `refusals` |
| `sa.build_alembic_baseline(...)` | what `baseline` calls: the Alembic project files plus the revision id |
| `sa.clear_cache()` | drops the cached `MetaData`; only useful in tests |

`values_for_create` / `values_for_update` are the least obvious and the most
important. They exist because `@default(cuid())` and friends are filled in by the
Prisma *client*, leave no trace in the DDL, and are therefore exactly the thing a
hand-written INSERT gets wrong in a way that looks like it worked. Some measured
details worth knowing before you build on them: `now()` is filled client-side
even though the column has a `DEFAULT CURRENT_TIMESTAMP`, because
`CURRENT_TIMESTAMP` is *transaction start* time and relying on it inside a
transaction backdates the row; timestamps are naive UTC for a `timestamp` column
and aware UTC for `@db.Timestamptz`, truncated to milliseconds; and `ulid()`,
`auto()` and `cuid(2)` are refused rather than guessed at.

`query_raw` and `execute_raw` need no translation at all — pass the same SQL to
`conn.execute(sa.text(...))`, converting positional `$1` parameters to named
`:name` binds. If your hot paths are already on `query_raw`, that part of the
migration is nearly free.

---

## What this page used to say, and no longer does

The previous version of this document predates all of the above. Three things
were removed rather than carried forward, because they could not be verified and
one of them was actively wrong:

- **An ORM translation table** mapping `create_many` to
  `session.execute(insert(User), [...])`, `group_by` to `select(...).group_by(...)`,
  and `batch_()` to a `session.flush()`. The first is the exact shape that
  silently drops keys (above); the second is on the STOP list; the third is wrong
  about what `batch_()` is — it is a transaction, and `engine.begin()` is what
  reproduces it. §4.1 replaces the whole table with translations that were
  executed.
- **"Memory profile" as a benefit.** Measured, it is a wash for a single client.
- **"You never need a flag day" / "reversible at every step."** The `tx()`
  whole-block rule and the STOP list both say otherwise. Coexistence is
  incremental at the granularity of a transaction, not a call site, and some
  modules do not move at all.

Claims about the identical `pg_dump` gate have been qualified rather than
deleted — see the note under the schema handover above for what the claim covers
and what it does not.
