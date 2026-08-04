---
name: prisma-to-sqlalchemy
description: Drive a phased migration of a Python codebase off Prisma Client Python onto SQLAlchemy, using the `prisma py sqlalchemy` doctor/generate/baseline commands and the project's verified-translation runbook. Use when someone wants to move, port, replace, or get off Prisma Client Python (`prisma.Prisma`, `db.user.find_many(...)`, `Model.prisma()`, `db.tx()`) in favour of SQLAlchemy Core, the SQLAlchemy ORM, or Alembic — including scoping or estimating that migration, running the doctor, translating call sites, handing DDL ownership to Alembic, or generating declarative models from a Prisma schema. Not for Prisma work that stays on Prisma (schema design, `prisma migrate`, query tuning), not for SQLAlchemy tasks with no Prisma involved, and not for Prisma's TypeScript/JavaScript client.
---

# Prisma Client Python → SQLAlchemy

You are driving a migration that has already been worked out. The translations
live in a runbook; the classification lives in a CLI. Your job is to **drive
them**, not to re-derive them and not to restate them.

The single governing rule: **a translation that is not in §4.1 of the runbook
does not get emitted.** Not "probably fine", not "obviously equivalent". §4.1's
rows were executed against a live PostgreSQL database with both clients and
compared row for row. Everything else is a STOP or a question.

## Before anything else: three questions

Do not read code, do not run the doctor, do not propose a plan until the first
two are answered. They change what every later phase means.

1. **Engine replacement or whole replacement?**
   - *Engine replacement* — call sites keep their Prisma-ish shape and a thin
     layer executes them over SQLAlchemy. Existing callers and tests are
     untouched; the diff is concentrated in a data-access module.
   - *Whole replacement* — the call sites themselves become SQLAlchemy, and
     everything that reads their return values moves too (see "return values
     are not the same objects" below — this is where that bites).

2. **Coexistence or cutover?**
   - *Coexistence* — Prisma and SQLAlchemy run side by side, indefinitely or for
     a long window. This is the only mode in which a partial migration is a
     finished state. It also makes the transaction rule load-bearing: the two
     clients **cannot** share a transaction.
   - *Cutover* — Prisma leaves. Then every STOP is a blocker to be solved by a
     human, not something to be recorded and walked past.

3. Once those two are answered, ask the third:
   **Generated declarative models, or the transparent `prisma.sa.metadata()`
   layer?**
   - `prisma py sqlalchemy generate` writes a declarative `models.py` — a real
     file, checked in, that you own from then on. Choose it when the destination
     is the ORM, or when the team wants source they can read and edit.
   - `prisma.sa.metadata()` / `sa.table_for('Post')` builds Core `Table`s at
     runtime from the generated client. No file, nothing to keep in sync, and it
     is what every §4.1 row is written against. Choose it for Core, and by
     default when unsure.
   - Either way `values_for_create` / `values_for_update` are still required —
     the generated models carry **no** client-side default generator for
     `cuid()`/`uuid()`/`@updatedAt` either. See the hard rules.

Record the three answers. They go in the final report, and a later phase that
contradicts them is a bug.

## Setup

**Find the runbook.** Everything below cites it, and its §4.1/§5 are the
authority. Try, in order:

```bash
python -c "import prisma, pathlib; print(pathlib.Path(prisma.__file__).parents[2])"   # editable install → docs/ is there
ls docs/prisma-to-sqlalchemy-runbook.md            # the project may vendor it
```

It is not shipped inside the wheel. If neither finds it, fetch
`docs/prisma-to-sqlalchemy-runbook.md` from the project's GitHub repository, and
say in your first message that you are working from it. If you cannot reach it
at all, `references/translations.md` here carries the §4.1 rows and the §5 list
— and the doctor's own `reason` and `reference` strings quote the runbook rows
verbatim, so classification still works. Say that you are degraded, and never
let a missing runbook become a licence to improvise.

**Confirm the tooling is the tooling.** Old copies of the runbook end with a
"What does not exist yet" section claiming there is no CLI. There is:

```bash
python -m prisma py sqlalchemy --help     # doctor / generate / baseline
python -c "from prisma import sa; print(sa.__version__)"
```

`sa.__version__` (`1.2.0` at time of writing) is independent of
`prisma.__version__` and is the only way to tell this fork from upstream.

## Phase 0 — measure

```bash
python -m prisma py sqlalchemy doctor --json > /tmp/doctor.json
```

`--path` is repeatable and defaults to the working directory; `--schema` is
discovered from `./schema.prisma`, `./prisma/schema.prisma` or `./prisma/`
(a *directory* under `prismaSchemaFolder` — all of its `.prisma` files count).
It reads source only: no database, no network, nothing written.

Read `references/doctor-report.md` for the exact JSON keys before parsing it.
The parts that decide the plan:

- `schema.scope_check.checks` — §0's hard stops. A `fail` on *provider is
  PostgreSQL* or *relationMode is not "prisma"* ends the migration here. Report
  it and stop.
- `schema.available` — `false` means the client was generated without
  `schemaMetadata = true`. Fix that first (Phase 1); `generate` and `baseline`
  raise `SchemaNotAvailableError` until you do. `doctor` itself still works.
- `schema.blockers` — schema-level STOPs, e.g. a self-referential implicit m2m.
- `summary` and `modules[].phase` — the slice.
- `caveats` — what the scan could **not** see. Read this out loud to the user.
  A report that is 60% `unknown` is the true answer for a codebase that builds
  its queries dynamically, and presenting it as "40% migratable" is a lie.

**State what the report says before you state what you will do.** The plan is a
consequence of the measurement, not a thing you bring to it.

## Phase 1 — schema metadata

Only if `schema.available` is `false`. Add to the generator block that the
application actually imports — not an environment variable, which leaks into
every generator block in the schema:

```prisma
generator client {
  provider       = "prisma-client-py"
  schemaMetadata = true
}
```

Then `prisma generate`, and re-run the doctor. Gate: `schema.available` is
`true` and `schema.tables` is non-zero.

## Phase 2 — Alembic owns DDL

No application code changes. Do **not** run `alembic init` and hand-edit
`env.py` per runbook §3 — `baseline` emits the same project with the
`include_object`, `compare_type` and `compare_server_default` work already done,
including the owned-sequence false positive that naive `compare_server_default`
gets wrong:

```bash
python -m prisma py sqlalchemy baseline --out migrations
# --models-import myapp.models   if you chose generated declarative models
```

It writes `migrations/env.py`, `migrations/script.py.mako`,
`migrations/versions/<rev>_baseline.py` — and **`alembic.ini` beside the `--out`
directory, not inside it**. Run `alembic` from wherever that `.ini` landed.

**The gate.** Autogenerate against the existing Prisma-managed database must
produce an empty migration:

```bash
alembic upgrade head          # the baseline adopts; it refuses an empty database
alembic revision --autogenerate -m "phase 2 gate"
```

`upgrade()` in the new file must contain only `pass`.

- Empty → correct. Delete the probe revision. Alembic owns DDL from here.
- Not empty → **STOP.** Do not edit the migration to make it empty. Report the
  diff verbatim and hand back.

Never run `prisma migrate` and `alembic upgrade` against the same database
afterwards; `_prisma_migrations` and `alembic_version` do not know about each
other.

## Phase 3+ — translate, one slice at a time

**The doctor picks the slice, not you.** In order:

1. Every module with `"phase": "migratable-now"` — every call site verified, no
   STOP, no unknown. This is the first phase, and it is chosen because it is the
   only set where nothing has to be decided.
2. Then `one-blocker` modules: translate the verified sites, leave the single
   STOP on Prisma, record it.
3. Then `mixed`, call site by call site.
4. `blocked` modules are not a phase. They are a conversation.

If `migratable-now` is empty, say so — do not promote a `mixed` module to fill a
plan-shaped hole.

For each call site in the slice:

- `status: "verified"` → translate it using the §4.1 row named in `reference`.
  `references/translations.md` has the rows; `assets/sa_helpers.py` has the
  three multi-line ones (`insert_many`, `upsert`, `conflict_target`) already
  written correctly.
- `status: "stop"` → **leave the Prisma call exactly as it is.** Do not attempt
  it, do not attempt "most of it". Record `path:line`, the operation, and the
  `reason` string, which is the §5 row.
- `status: "unknown"` → **ask the user.** Quote the `reason`. An opaque `data=`
  is not a small gap: `data` could hold `{'posts': {'create': [...]}}` (a nested
  write, §5) or `{'n': {'increment': 1}}` (atomic, §5), and the scanner says so
  rather than guessing. Ask what it holds; do not open the caller and infer.

Migrate a call site at a time and assert equivalence as you go:

```python
old = await db.post.find_many(where=..., take=...)
new = conn.execute(sa.select(post).where(...).limit(...)).mappings().all()
assert sorted(p.id for p in old) == sorted(r['id'] for r in new)
```

**Gate, after each phase — not at the end:** the project's own test suite passes
unmodified, and `alembic revision --autogenerate` is still empty. If the schema
did not change, the second is cheap; run it anyway, because a translation that
quietly needed a column is exactly what it catches.

## Hard rules

Every one of these was learned by measurement. None is a style preference.

1. **A `tx()` block migrates whole, or stays on Prisma whole.** A half-migrated
   block is provably two transactions on two connections that cannot see each
   other; rolling back the SQLAlchemy half leaves the Prisma half committed.
   `transactions[].blocked` in the doctor JSON already applies this — one STOP
   inside a block turns every member into a STOP.

2. **A call site handed a `Connection` must not call `conn.begin()`.** A
   `Connection` autobegins on its first statement, so a helper that was
   `async with db.tx():` and becomes `conn.begin()` raises
   `InvalidRequestError: … already initialized a SQLAlchemy Transaction()` the
   moment its caller has already used the connection. Take the `Connection` as a
   parameter and let the caller own the boundary; `begin_nested()` is the only
   inner boundary that composes.

3. **Never write a bare INSERT.** `values_for_create` / `values_for_update` fill
   in what Prisma fills in *client-side* — `cuid()`, `uuid()`, `nanoid()`,
   `now()`, `@updatedAt`. None of those leaves a trace in the DDL, so a bare
   `sa.insert(post).values(slug=...)` sends no id and no `updatedAt` and writes a
   row Prisma would not have written. This applies to the generated declarative
   models too: they carry no Python-side default generator either.

4. **`sa.insert(t).values([...])` compiles its VALUES clause from the *first*
   mapping** and silently drops keys only later rows carry — no error, right row
   count, wrong data. Group rows by key set and issue one INSERT per group; use
   `insert_many` from `assets/sa_helpers.py`.

5. **`rowcount` is `-1` for INSERT under psycopg.** Counts come from
   `.returning(*table.primary_key.columns)` and `len(...)`. `rowcount` *is*
   correct for UPDATE and DELETE.

6. **A `@map`ped enum in a `where` needs the stored label**, not the schema
   member name — `enum_label('Role', 'ADMIN')` → `'administrator'`. Binding
   `'ADMIN'` is rejected by the enum type outright. `values_for_*` does this for
   the `data` side; the `where` side is yours.

7. **Never weaken a test to make a phase pass.** A failing test after a
   translation means the translation is wrong or the call site was a STOP.
   Changing an assertion, loosening a comparison, or marking an xfail to get
   green is a hand-back, not a fix.

Two more that come up constantly:

- **Use `.first()`, not `.one()`** on a `RETURNING` from `update`/`delete`.
  Prisma returns `None` on no match; `.one()` raises.
- **Return values are the same row, not the same Python objects.** Prisma hands
  back the enum *member* and an aware UTC datetime; `RETURNING` hands back the
  stored label and a naive datetime for `timestamp without time zone`. Under
  whole replacement this is a call-site change, not a translation.

## Stop and hand back

Stop — do not work around, do not pick the likelier reading — when:

- A §0 scope check fails (non-PostgreSQL provider, `relationMode = "prisma"`).
- The Phase 2 autogenerate diff is not empty.
- A call site is `unknown` and the user has not answered what it contains.
- A `stop` call site sits inside a `tx()` block you were asked to migrate.
- `sa.metadata()` raises naming a `@db.*` annotation, or `values_for_*` raises
  `LookupError` naming a relation field.
- A test fails and the only way to green is to change the test.

Hand back with: the specific call site, the `reference` string, and what you
would need to proceed. Not a paraphrase, not a suggestion to "just try it".

## The deliverable

The report, not the diff:

```markdown
## Migration report

Mode: <engine | whole> replacement, <coexistence | cutover>, <declarative | Core metadata>
Scope check: <provider> / relationMode <value> / @db.* usage: <count>
Alembic baseline: <empty | NOT EMPTY — diff below>

Call sites: <n> found, <n> translated, <n> left on Prisma, <n> awaiting an answer

### Translated
<file:line — operation — verified equal: yes/no>

### Left on Prisma (STOP)
<file:line — operation — reason, quoted from §5>

### Requires human decision
<every `unknown`, and everything the scope check flagged>
```

Under cutover, a migration with any call site still on Prisma is **partial**.
Say partial, with the count. Never report it as complete.

## Reference files

- `references/doctor-report.md` — the `doctor --json` shape, key by key, with
  the phase and caveat vocabularies. Read before parsing the report.
- `references/translations.md` — §4.1's rows and §5's list, condensed, with the
  measured caveats attached to the rows they apply to.
- `references/phases.md` — what each answer to the three questions actually
  changes; the per-phase gates in full; the equivalence harness.
- `assets/sa_helpers.py` — `insert_many`, `upsert`, `conflict_target`, and the
  engine/driver setup, in their verified form. Copy into the project; do not
  retype from memory.
