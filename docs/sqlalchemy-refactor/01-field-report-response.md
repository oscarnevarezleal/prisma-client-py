# Response to the adoption field report

A client ran `docs/prisma-to-sqlalchemy-runbook.md` as written against a
182-model production schema (62 enums, 432 migrations, ~2,100 call sites) and
reported five defects and nine documentation issues. Their verdict: *"the
migration cannot start"* — `sa.metadata()` raised on 176 of 182 models.

All five defects are fixed and covered by tests. This is the triage, what was
done about each, and the two structural lessons.

The report was accurate throughout. Every defect reproduced on the first
attempt, and the one inference it drew about our test fixtures was correct in
substance.

---

## Defects

| # | Defect | Status | Now covered by |
| --- | --- | --- | --- |
| B5 | non-PK `autoincrement()` silently loses its `SEQUENCE` | **fixed** | `test_b5_*`, DDL gate |
| B2 | `uuid(4)` missing from the generator allowlist — blocks every schema using it | **fixed** | `test_b2_*`, DDL gate |
| B1 | `@db.*` native types invisible | **fixed** — now honoured, not just documented | `test_b1_*`, DDL gate |
| B3 | identifiers exceed PostgreSQL's 63-char limit | **fixed** | `test_b3_*`, DDL gate |
| B4 | empty scalar-list default emits uncastable `ARRAY[]` | **fixed** | `test_b4_*`, DDL gate |

### B5 — the one that mattered most

Ranked first in the report and correctly so: it is the only defect that produced
a database passing the runbook's own gate and then rejecting writes.

`SERIAL` is what SQLAlchemy emits from `autoincrement=True`, and it only applies
to an integer **primary key**. On any other column SQLAlchemy treats a
`Sequence` as a client-side pre-execute default and emits no DDL default at all,
so the column lands `NOT NULL` with nothing to fill it.

Fixed by naming the sequence in the metadata (`<table>_<column>_seq`, verified
against `prisma db push`) and, in the builder, emitting three things SQLAlchemy
does not give you together:

- `CREATE SEQUENCE … AS integer` — sized to the column, since SQLAlchemy
  defaults to bigint and `Int` columns would otherwise all diff
- `DEFAULT nextval('"…"'::regclass)` as an explicit server default
- `ALTER SEQUENCE … OWNED BY …`, which SQLAlchemy has no API for at all, via a
  DDL event — without it the sequence outlives a dropped column

### B1 — fixed rather than documented harder

The report suggested refusing at `prisma generate` time, on the grounds that
`@db.*` never reaches the generator payload. Refusing would have been the
consistent thing to do, and it was the right call to propose.

We did better than refuse, because the premise turned out to be too pessimistic.
`@db.*` does not reach the **DMMF** — but `GenericData.datamodel` carries the
raw schema text, and Prisma concatenates it across a `prismaSchemaFolder`. So
the annotations are recoverable by lexing (`generator/_native_types.py`), and
`@db.Uuid`, `@db.VarChar(n)`, `@db.Timestamptz(p)` and the rest now produce the
right column type.

An annotation with no mapping raises naming itself rather than falling back to
the default type — the philosophy the report cited, applied where it belongs.

### B2, B3, B4

- **B2**: `@default(uuid())` arrives as `uuid(4)`; the schema text and the DMMF
  disagree. The version is now split off into its own field, so consumers match
  on the generator. `cuid(2)` and `uuid(7)`, which the report predicted would
  fail next, are covered by the same change.
- **B3**: Prisma truncates to 63 characters and keeps the suffix. The exact
  name from the report is now a test case.
- **B4**: `ARRAY[]::uuid[]`, following the `@db.*` annotation when present.

---

## Documentation issues

All nine addressed. The ones that were more than wording:

| # | Change |
| --- | --- |
| D1 | §0 now locates the schema first and handles `prismaSchemaFolder`; every grep covers `*.prisma` |
| D2 | Explained: `PRISMA_PY_CONFIG_*` env vars are **process-wide**, so they apply to every generator block. Per-block config does not leak — verified. |
| D3 | Driver is no longer rewritten unconditionally; psycopg2 is the SQLAlchemy default and most applications ship it |
| D4 | `join_ambiguous` is now emitted on **every** relation, `False` where trivially so. The instruction to check it is now safe to follow. |
| D5 | §3 shows the `include_object` hook that stops autogenerate proposing `DROP TABLE _prisma_migrations` |
| D6 | Server defaults and sequences added to the "necessary but not sufficient" list, with `compare_server_default=True` |
| D7 | `prisma.sa.__version__` added; §0 checks that `sa.metadata()` *builds*, not merely that `prisma.sa` imports |
| D8 | §0 shows how to read the pinned CLI version and why `npx prisma` unpinned gives an unexplained `P1012` |
| D9 | §2 warns that an editable install redirects generated output into the library checkout |

D6 is the sharpest of these. The report's phrasing — *"The library gets every
item that is on that list right, and misses the one that isn't"* — is exactly
right, and it is a general lesson about caveat lists: the failure you did not
think to list is the one that gets you.

---

## Two structural lessons

### The fixture only tests what it contains

The report inferred that our fixtures use hand-written DMMF rather than output
from the pinned CLI. Not quite — the fixture *is* recorded from a real
`prisma generate` — but the conclusion was right anyway, for a reason that is
worse: the recorded schema simply contained none of these five shapes. No
`uuid()`, no `@db.*`, no non-PK autoincrement, no empty list default, no long
identifier. A real recording of a schema that exercises nothing proves nothing.

The reference schema now carries all five, so `test_ddl_equivalence.py` — which
builds one database with `prisma db push` and another with `create_all()` and
requires `pg_dump` to agree — covers them against a real database on every run
with PostgreSQL available.

### "Identical dumps" was a claim about one schema

The docs said the two dumps come out identical. On the reporting client's schema
they did not: 2,191 lines of diff, fully explained by B1 and B5.

Both statements were true of the schema we tested. Neither was true in general,
and the docs did not distinguish. What makes the claim mean anything is the
breadth of the schema behind it, so the reference schema is now the thing to
extend when a new shape appears — not the claim to soften.

---

## What is still true from the report

Two findings stand and are not defects:

**62% of call sites are on the STOP list.** Writes and transactions are the
entire mutation surface. The report's own conclusion — *"we would rather have it
than a guessed write path"* — is the right reading, and their prioritisation
follows: the near-term value of `prisma.sa` is Phase 3, Alembic owning DDL,
which is exactly what B1 and B5 blocked. Both are now fixed. Verified write
translations are the next piece of work.

**PostgreSQL only.** Unchanged.
