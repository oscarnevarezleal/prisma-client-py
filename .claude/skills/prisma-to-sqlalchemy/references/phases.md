# The three answers, and the gates

## What the three scoping questions actually change

### 1. Engine replacement vs whole replacement

|  | engine replacement | whole replacement |
| --- | --- | --- |
| what moves | one data-access module | every call site, and everything that reads its results |
| call sites | keep a Prisma-ish signature | become SQLAlchemy |
| return values | you convert back to what callers expect | callers change |
| tests | mostly unchanged | change with the call sites |
| STOP handling | the shim can keep a Prisma path per method | a STOP is visible in application code |

Engine replacement is where the **return-value differences** get absorbed
deliberately: a `RETURNING` row hands back the stored enum label and a naive
datetime, so a shim that promises Prisma's shape must map them back:

```python
row['role'] = Role(row['role'])                                   # stored label -> member
row['createdAt'] = row['createdAt'].replace(tzinfo=timezone.utc)  # naive UTC -> aware
```

Whole replacement is where they get *propagated*. Either is fine; silently
doing neither is the failure — the comparison that used to match stops matching
under `@map`, and nothing raises.

Engine replacement also collides with hard rule 2. A shim method that used to
open `db.tx()` must **not** call `conn.begin()`: take the `Connection` as a
parameter and let the caller own the boundary.

### 2. Coexistence vs cutover

**Coexistence** is the only mode where a partial migration is a finished state.
It makes these load-bearing:

- Two connection pools, two sets of limits. `max_wait` (2s) is Prisma's;
  `pool_timeout` (30s) is SQLAlchemy's default. Set `pool_timeout` on
  `create_engine` deliberately rather than inheriting it.
- **No transaction spans both.** Any `tx()` block is either fully Prisma or
  fully SQLAlchemy, forever, on either side of any phase boundary.
- Alembic owns DDL from Phase 2 on. `prisma migrate` must not be run against
  that database again — `_prisma_migrations` and `alembic_version` do not know
  about each other. Prisma keeps *reading* the schema fine.
- `prisma generate` still has to run, because `prisma.sa.metadata()` is built
  from the generated client.

**Cutover** turns every STOP into a blocker a human has to solve, not a line in
a report. Under cutover you must never report "complete" while any call site is
still on Prisma — report partial, with the count, and list what is left.

### 3. Declarative models vs the Core metadata layer

|  | `generate` (declarative) | `prisma.sa.metadata()` (Core) |
| --- | --- | --- |
| artefact | a `models.py` you own | none |
| kept in sync by | you, after every schema change | nothing — built at runtime |
| §4.1 rows written against | no | **yes** |
| `Session` available | yes | no — Core `Table`s only |
| refusals | shapes it cannot express are left out and named on stderr | n/a |

```bash
python -m prisma py sqlalchemy generate --out myapp/models.py
python -m prisma py sqlalchemy generate --strict   # fail instead of omitting a shape
```

Source goes to stdout when `--out` is omitted; refusals always go to **stderr**
so stdout stays pipeable. Exit code is 0 with refusals unless `--strict`. Read
the refusals — a self-referential implicit m2m is emitted as a comment where the
attribute would have been, so the absence is visible in the file too.

Both paths need `values_for_create` / `values_for_update`. The generated models
carry `server_default` where the *database* has a default and **nothing** where
Prisma fills the value in client-side, which is `cuid()`, `uuid()`, `nanoid()`,
`now()` on `@default`, and every `@updatedAt`. Choosing declarative does not buy
you out of hard rule 3.

Choosing declarative also changes Phase 2:

```bash
python -m prisma py sqlalchemy baseline --out migrations --models-import myapp.models
```

which points `env.py` at `Base.metadata` from your module instead of
`sa.metadata()`.

---

## Phase gates, in full

A gate is run **at the end of its phase**, before the next one starts. A phase
whose gate did not pass is not finished, and its work does not get built on.

### Phase 1 — schema metadata

```bash
python -c "from prisma import sa; print(sa.__version__, len(sa.metadata().tables), 'tables')"
python -c "from prisma import sa; print(sa.table_for('<AnyModel>').name)"
```

Both must succeed. Importing `prisma.sa` is **not** the check — it imports
cleanly on a client whose metadata cannot be built, so the import passes and
Phase 2 then fails.

If `sa.metadata()` raises naming a `@db.*` annotation, that is a STOP: report
the annotation. If it raises `SchemaNotAvailableError`, `schemaMetadata = true`
did not take effect — check you edited the generator block that actually runs
(`grep -rnE '^generator\s+\w+' <schema dir>/*.prisma` against
`grep -rn 'from prisma import' --include='*.py' .`) and re-run `prisma generate`.

Under an editable install, `prisma generate` without an explicit `output` writes
into the *library* checkout rather than the project. Set `output` explicitly on
every generator block.

### Phase 2 — Alembic

```bash
python -m prisma py sqlalchemy baseline --out migrations
alembic upgrade head
alembic revision --autogenerate -m "phase 2 gate"
```

`upgrade()` in the generated probe revision must contain only `pass`. Then delete
the probe.

- The baseline revision **refuses an empty database** rather than stamping it —
  it inspects for its `EXPECTED_TABLES` and raises if they are missing. That is
  deliberate: stamping an empty database as migrated makes every later revision
  run against a schema that is not there.
- The revision id is derived from the schema, not the clock, so regenerating
  rewrites the file instead of adding a second baseline.
- `alembic.ini` lands **beside** `--out`, not inside it. `--out migrations` puts
  it in the current directory with `script_location = migrations`.
- `sqlalchemy.url` in the generated `alembic.ini` is deliberately empty; `env.py`
  reads `DATABASE_URL`.

**Not empty is a STOP.** Do not edit the migration to make it empty — a
non-empty diff means the metadata and the database disagree, and applying it
would alter a production schema. Report the diff verbatim.

An empty diff is **necessary but not sufficient.** Autogenerate does not compare
FK `ondelete`/`onupdate`, constraint names, index methods or per-column sort
order, or column order. The generated `env.py` already turns on `compare_type`
and a `compare_server_default` that suppresses only the owned-sequence false
positive — a naive one either misses a column that lost its `DEFAULT nextval(...)`
(and then rejects every INSERT) or evaluates `nextval()` twice per run and burns
sequence values.

Stronger check, if a scratch database is available:

```bash
python -c "
from prisma import sa; import sqlalchemy
sa.metadata().create_all(sqlalchemy.create_engine('$SCRATCH_URL'))"
pg_dump --schema-only --no-owner --no-acl "$PRODUCTION" > /tmp/a.sql
pg_dump --schema-only --no-owner --no-acl "$SCRATCH"    > /tmp/b.sql
diff /tmp/a.sql /tmp/b.sql
```

Compare against a database built by `prisma migrate deploy`, and expect noise
that is not the library's: run `prisma migrate diff` first to measure the
project's own schema-versus-migrations drift, and subtract it.

### Phase 3+ — each translation slice

Two gates, both every phase:

1. **The project's test suite passes, unmodified.** If the only way to green is
   to change a test, the translation is wrong or the call site was a STOP. Hand
   back. (Hard rule 7.)
2. **`alembic revision --autogenerate` is still empty.** Cheap when the schema
   did not change, and it is exactly what catches a translation that quietly
   needed a column.

Plus, per call site, an equivalence assertion while both clients are still
present:

```python
old = await db.post.find_many(where=..., take=...)
new = conn.execute(sa.select(post).where(...).limit(...)).mappings().all()
assert sorted(p.id for p in old) == sorted(r['id'] for r in new)
```

Compare on **ids**, or normalise first. Comparing whole rows will fail on the
two known-and-intended differences: a `@map`ped enum comes back as the stored
label rather than the member, and a `timestamp without time zone` comes back
naive rather than aware UTC.

For a write, run the Prisma call and the SQLAlchemy call against separate rows
and compare the rows, not the return values. They will not be the same objects
even when they are the same row.

---

## Scope facts to collect once, for the report

The doctor gathers all of these; this is what they mean.

| fact | source | why |
| --- | --- | --- |
| provider | `schema.scope_check.provider` | non-PostgreSQL ends the migration |
| relationMode | `schema.scope_check.relation_mode` | `"prisma"` means no FKs at all; the DDL is emitted correctly for it, but nothing enforces the relations afterwards — ends it |
| `@db.*` census | `schema.scope_check.native_types` | how much of the schema depends on the lexer |
| schema files | `schema.scope_check.paths` | under `prismaSchemaFolder` this is several files, all of which count |
| models / tables / enums / join tables | `schema.*` | join tables are the implicit m2m ones Prisma manages without a model |
| relation shapes | `schema.relation_shapes` | m2m counts predict how much of §4.1's Includes section applies |

**Pin the Prisma CLI version.** The library pins the CLI it was built against;
an unpinned `npx prisma` resolves to whatever is current and rejects the schema
with `P1012` and no hint that the cause is version skew:

```bash
python -c "from prisma import config; print(config.prisma_version)"
```

Pin the same version in `package.json`, and use `python -m prisma` rather than
`npx prisma` so the pinned CLI is the one that runs.
