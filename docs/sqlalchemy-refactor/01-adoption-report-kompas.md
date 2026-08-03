# Adoption report — first real-world consumer

Field report from running [`prisma-to-sqlalchemy-runbook.md`](../prisma-to-sqlalchemy-runbook.md)
against a production Prisma Client Python codebase: 182 models, 62 enums, 1 view,
432 migrations, ~2100 Prisma call sites, PostgreSQL 16.

Nothing here is a fix. It is a list of what stopped us, with minimal
reproductions, so the failures can be turned into tests.

**Headline:** `sa.metadata()` raises before returning a single table on any
schema that uses `@default(uuid())` — which is 176 of our 182 models. The
runbook's Phase 2 verification command fails, so Phases 3–5 are unreachable
without patching the library at runtime.

**Also worth knowing:** the fork is a flawless drop-in as a Prisma client. Two
suites, 2790 tests, zero deltas against upstream `0.15.0`. Every problem below is
confined to the new `prisma.sa` surface.

---

## B2 — `uuid(4)` is not in `_CLIENT_SIDE_GENERATORS` (blocks everything)

```console
$ python -c "from prisma import sa; sa.metadata()"
NotImplementedError: Unhandled Prisma default generator: uuid(4)()
```

`src/prisma/sa/_build.py`:

```py
_CLIENT_SIDE_GENERATORS = frozenset({'cuid', 'uuid', 'nanoid', 'ulid', 'auto'})
```

The schema text says plain `@default(uuid())`, but the DMMF emitted by
**Prisma CLI 5.19.0 — the version `_config.py` pins** — normalises it to the
versioned form:

```py
>>> from prisma._schema import model_schema
>>> model_schema('Journey')['fields']['id']['default']
{'kind': 'generator', 'name': 'uuid(4)', 'args': []}
```

So the allowlist never matches and `_server_default` falls through to the
`raise`. Same class of problem presumably awaits `cuid(2)` and `uuid(7)`.

This is severe because `@default(uuid())` is the single most common id strategy
in Prisma schemas, and because it fails at the *first* step that touches the
library. That it survived the package's own `db push` vs `create_all` gate
suggests the test fixtures are hand-written DMMF rather than output from the
pinned CLI — worth checking, since a fixture that disagrees with the pinned CLI
will keep hiding this class of bug.

---

## B3 — emitted identifiers exceed PostgreSQL's 63-character limit

```console
sqlalchemy.exc.IdentifierError: Identifier
'document_requirements_on_documents_document_id_document_requirement_id_key'
exceeds maximum length of 63 characters
```

Raised by SQLAlchemy during `alembic revision --autogenerate`, so it blocks
Phase 3 outright.

Prisma truncates; the library does not. For the same constraint Prisma created:

```
document_requirements_on_documents_document_id_document_req_key   (exactly 63)
```

10 identifiers in our schema exceed the limit; the longest emitted is 81 chars
(`journey_order_instruction_evidence_log_journey_order_instruction_evidence_id_fkey`).

Given that naming is otherwise reproduced *exactly* (see "what matched" below),
this reads as a missing truncation rule rather than a design gap.

---

## B4 — empty scalar-list default emits an uncastable `ARRAY[]`

```console
psycopg2.errors.IndeterminateDatatype: cannot determine type of empty array
LINE 9:  read_by TEXT[] DEFAULT ARRAY[],
HINT:  Explicitly cast to the desired type, for example ARRAY[]::integer[].
```

From `readBy String[] @default([]) @db.Uuid @map("read_by")`. Prisma emits
`ARRAY[]::uuid[]`; the library emits bare `ARRAY[]`, which PostgreSQL rejects.

`_literal_sql` already casts *enum* arrays correctly —

```py
return f'ARRAY[{labels}]::"{enum.name}"[]'
```

— but the scalar branch has no equivalent. `create_all()` aborts on the first
such table. One occurrence in our schema was enough to stop the phase.

---

## B5 — non-PK `autoincrement()` silently loses its SEQUENCE ⚠️

The one we would most want fixed, because it is invisible.

`_server_default` returns `None` for `autoincrement`:

```py
if name == 'autoincrement':
    # SERIAL, which SQLAlchemy emits from `autoincrement=True`
    return None
```

That holds for **integer primary keys**. On a non-primary-key column SQLAlchemy
emits plain `integer` and no sequence. Our schema has 16 such columns
(`journey.journey_number`, `attachments.attachment_number`,
`orders.order_number`, …), all `Int @default(autoincrement())`, all `NOT NULL`:

| | `prisma db push` | `metadata().create_all()` |
| --- | --- | --- |
| sequences in schema | 16 | **0** |
| `attachments.attachment_number` default | `nextval('…_seq'::regclass)` | *(none)* |
| nullable | NO | NO |

The result is a column that is `NOT NULL` with no default, so **every INSERT into
those 16 tables fails**.

What makes it dangerous rather than merely wrong: **Alembic autogenerate cannot
see it.** `compare_server_default` is off by default, and our 5766-line baseline
migration mentions `attachment_number` exactly zero times. An adopter who works
around B1–B4, gets a clean autogenerate, stamps it and cuts over ends up with a
database that passes the runbook's gate and rejects writes.

---

## B1 — `@db.*` blindness is documented, but its severity is understated

Known and stated in the docs, so not a bug report — a calibration note.

Our schema has 570 `@db.*` annotations (550 `@db.Uuid`, 20 `@db.VarChar(n)`).
In the `pg_dump` comparison that is **554 columns** typed wrongly:

```
533 columns:  uuid       → text
 21 columns:  varchar(n) → text
```

Every primary and foreign key in the application. `migrating-to-sqlalchemy.md`
advises "if you use them, diff once after cutover" — but a `uuid` → `text`
conversion across 550 columns is not a post-cutover diff, it is a full-database
rewrite with index and FK rebuilds on every table.

**Suggestion:** `sa.metadata()` genuinely cannot detect this, since `@db.*` never
reaches the generator payload. But the *generator* can read the raw schema text.
Refusing at `prisma generate` time — the same way an unverified provider raises
`UnsupportedProviderError` rather than emitting a plausible schema — would match
the philosophy already stated in the docs and would have saved us the entire
Phase 3 investigation.

---

## What matched exactly

Stated because it is the hard part, and because the diff below is what remains
after B1 and B5 are normalised away — nothing else diverged.

| | `db push` | `create_all` | |
| --- | --- | --- | --- |
| Tables | 182 | 182 | ✅ |
| Enum types (with labels) | 62 | 62 | ✅ |
| Indexes | 495 | 495 | ✅ identical names |
| FK referential actions | — | — | ✅ |
| Column order, nullability, precision | — | — | ✅ |

Constraint and index naming is faithful, including Prisma's non-obvious
`CREATE UNIQUE INDEX`-for-uniques behaviour — and that is exactly the class of
difference Alembic autogenerate would *not* have caught. The documented
`RESTRICT`-for-mandatory / `SET NULL`-for-optional FK default matched the
database everywhere.

Raw `pg_dump` diff was 2191 lines. After normalising `uuid`/`varchar` → `text`
and stripping sequences, **125 lines remain — all of them the same sequences,
plus one cosmetic `::character varying` → `::text` cast.**

---

## Documentation feedback

**D1 — the runbook assumes a single `schema.prisma`.** We use
`prismaSchemaFolder`: 63 files, 5 generator blocks (2 × `prisma-client-py`,
2 × `prisma-client-js`, 1 disabled). Phase 2's "check you edited the generator
block that actually runs" has no answer when two Python clients are generated
and the one the application's repositories actually use is the *async* one. All
of §0's scope-check greps also assume one file.

**D2 — `schemaMetadata` leaks across generator blocks.** We set it on `client`
only. `async_client`, which never opted in, received a 35 KB `metadata.py` with a
full payload and its `sa.metadata()` passes the availability check. Convenient,
but undocumented, and there is no way to opt a client *out* of the weight.
(`metadata.py` is emitted unconditionally; the flag only controls the payload.)

**D3 — driver mismatch.** §4 hardcodes
`.replace('postgresql://', 'postgresql+psycopg://')` (psycopg 3). Our app, like
most, ships `psycopg2-binary`; copied verbatim this gives
`ModuleNotFoundError: No module named 'psycopg'`.

**D4 — `relation(...)['join_ambiguous']` does not exist.** §5 instructs the agent
to check it before traversing a self-referential implicit m2m. It is present on
**0 of 640** relations here and raises `KeyError`. If it is attached only to
implicit m2m relations, say so and show `.get('join_ambiguous')`.

Everything else in §4.1/§5 that we exercised was accurate: `fk_required`,
`fk_model`, `fk_columns`, `referenced_columns`, `model_schema(...)['uniques']`,
and `table_for` correctly raising `LookupError` when handed a table name.

**D5 — §3 does not warn that autogenerate proposes `DROP TABLE _prisma_migrations`.**
It is not in the metadata, so Alembic wants it gone. The runbook says not to edit
the migration — but applying this one destroys the Prisma migration history you
still need for the whole transition.

**D6 — the "necessary but not sufficient" list omits the failure that matters.**
§3 lists what autogenerate misses: FK `ondelete`/`onupdate`, constraint names,
index methods, column order. It does not mention **server defaults or sequences**
— which is B5, the only silent, data-rejecting divergence we found. The library
gets every item that *is* on that list right, and misses the one that isn't.

**D7 — the fork is indistinguishable from upstream by version.** Both report
`0.15.0`. §0's check ("`pip show prisma` → editable/fork, or `prisma.sa`
importable") is the only signal, and `prisma.sa` imports cleanly while
`sa.metadata()` still crashes — so the scope check passes on a client that cannot
work. A local version segment (`0.15.0+sa.1`) or `prisma.sa.__version__` would
make this checkable.

**D8 — no statement of the supported Prisma CLI range.** The library pins 5.19.0.
Our repo invoked `npx prisma` unpinned, which now resolves to **7.9.1** and
rejects the schema with `P1012` (`datasource.url` no longer supported) — nothing
in that error hints the cause is a CLI version. Separately, `prisma generate`
writes `"prisma": "^5.17.0"` and `"@prisma/client": "^5.17.0"` into
`package.json` unasked, then warns that 5.19.0 and 5.17.0 disagree. One line in
the runbook would save an adopter an hour.

**D9 — an editable install redirects generated output into the library checkout.**
With `prisma = { path = "…", editable = true }` — the natural way to trial a fork —
`prisma generate` writes the *application's* client into
`../prisma-client-py/src/prisma/`. Gitignored, so harmless, but surprising, and
two applications cannot trial the fork at once.

---

## Where the runbook's own STOP list leaves us

Independent of the bugs, the documented path does not reach a migration for an
application shaped like ours. Call-site inventory (2092 total):

| | Count | |
| --- | --- | --- |
| Writes — `create`/`update`/`delete`/`upsert` + `_many` | 1288 | ⛔ §5 |
| Transactions (`.tx`) | 34 | ⛔ §5 (overlaps above) |
| Reads — `find_*`, `count` | 751 | ✅ |
| Raw SQL | 53 | ✅ passthrough |
| `aggregate` / `group_by` | 0 | — |

**62% of call sites are on the STOP list before any work begins.** Writes and
transactions are the entire mutation surface of the application, so even a
flawless `prisma.sa` migrates reads and leaves both stacks running indefinitely.

That is an honest consequence of only shipping verified translations, and we
would rather have that than a guessed write path. But it means the realistic near
-term value of `prisma.sa` is **Alembic owning DDL** (Phase 3) rather than query
migration (Phase 4) — and Phase 3 is exactly what B1 and B5 block. If the goal is
adoption, those two are the highest-leverage fixes.

---

## Reproduction

Full spike, including the Alembic setup and the runtime shims used to get past
B2/B3/B4, is in the consuming repo at `spikes/sqlalchemy-migration/`
(`kompas-mx/backend`, branch `claude/prisma-sqlalchemy-migration-4zxd6e`).

Minimal repro for B2 needs only a schema with `@default(uuid())`:

```prisma
generator client {
  provider       = "prisma-client-py"
  schemaMetadata = true
}
model Thing {
  id String @id @default(uuid())
}
```

```console
$ prisma generate && python -c "from prisma import sa; sa.metadata()"
NotImplementedError: Unhandled Prisma default generator: uuid(4)()
```
