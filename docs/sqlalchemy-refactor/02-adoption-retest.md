# Response to the adoption retest

The client re-ran the migration against `22f9337` on the same 182-model
production schema. Four of five defects confirmed fixed **against a live
database** rather than against the new tests — the right way to check, given
that the original defects were precisely the shapes the fixtures did not
contain.

Three items came back. All three are now fixed and, more importantly, are in
the reference schema.

| | before | after 1.1.0 | after 1.2.0 |
| --- | ---: | ---: | ---: |
| `pg_dump` diff | 2,191 lines | 56 | **0 expected** |
| Alembic baseline | 5,766 lines | 78 | **empty expected** |
| shims to reach Phase 3 | 3 | 1 | **0** |

The "expected" column is what the reference-schema equivalent now produces
here; the client's own schema is the real measurement and we would like the
re-run.

---

## The three

### B3 remainder — foreign key names were not truncated

The first fix truncated uniques and indexes. Foreign keys kept deriving an
untruncated `<table>_<columns>_fkey`, so 7 of the reporter's 10 long identifiers
still raised `IdentifierError` before any SQL was sent.

Their framing of this is the accurate one and worth repeating:

> This is the response's own lesson, recurring inside its own fix. The report's
> example was a `_key`; it became the test case; the `_fkey` path was never
> exercised.

Fixed by moving the constraint name into the metadata, where the truncation rule
already lived, rather than deriving it in the builder. A foreign key whose
derived name overflows 63 characters — the shape they reported, on a
stand-in schema rather than theirs — is now a test case.

### B6 — `sort: Desc` dropped from indexes

`sortOrder` **was in the DMMF all along** and the builder simply ignored it. No
lexing needed; a one-line omission.

Their analysis of why it matters is correct and is why this is not cosmetic. On
a single-column index the direction is nearly free, since PostgreSQL scans
backwards. On a composite index it is not: `(a ASC, b ASC)` read backwards is
`(a DESC, b DESC)`, which serves neither `ORDER BY a, b DESC` nor
`ORDER BY a DESC, b`. 11 of their 12 were composite `(scope, timestamp DESC)`
pairs — the "latest N for this org" paging shape — so the index the schema asked
for could not be substituted by the one that was built, and those queries fall
back to a sort node.

### B7 — `@relation(map: "…")` ignored

Absent from the DMMF entirely, like `@db.*`, so it is now lexed from the raw
schema text by the same module.

Their note on the trap is exact:

> the bug is invisible except when `map:` is doing something, which is the only
> reason to write it

Two of their three uses coincided with the derived name. And Alembic does not
compare constraint names, so this was silent to the gate — the same class as B5.

---

## The lesson, third time

The retest is evidence for the first structural lesson, not against it:

> "The fixture only tests what it contains." B6 and B7 are two more shapes the
> reference schema does not contain … Neither is exotic; both are ordinary
> Prisma.

Both are ordinary Prisma, and both got through a suite that had just been
extended specifically to stop this. That is the point: extending the fixture
*reactively*, one report at a time, converges slowly. Each round finds the
shapes the previous round did not think of.

The reference schema now covers eleven shapes across four rounds:

| round | shapes added |
| --- | --- |
| initial | `@@map`, `@map`, compound `@@id`, four relation shapes, enums with `@map`, self-referential m2m |
| 1.1.0 | `uuid()` → `uuid(4)`, non-PK `autoincrement()`, `@db.*`, long `_key`, empty list default |
| 1.2.0 | long `_fkey`, `sort: Desc`, `@relation(map:)` |
| 1.3.0 | `@relation(onUpdate:)`, in all four non-default actions |

The fourth round was found here rather than reported, and it is the same shape
for the fourth time: `onUpdate` is in the schema language, absent from the DMMF,
and the reference schema declared none — so the emitter's hardcoded
`ON UPDATE CASCADE` matched the `pg_dump` gate exactly, on every relation it
contained. A fixture only tests what it contains, and a gate is only as strong
as the fixture behind it.

### On their offer

> We are happy to re-run this against any future commit — the harness is
> standing and takes about ten minutes.

That is worth more than any fixture we can write, and it is the right answer to
the convergence problem above: a real schema as a **recurring** test input, not
a one-off report. A synthetic reference schema only ever contains what someone
thought to put in it. Theirs contains what an application actually needs.

Concretely, what would help most, in order:

1. **The re-run against this commit.** If the diff is not zero, the remainder is
   the next round.
2. **Their schema as a recorded DMMF fixture**, if they are willing — the
   generator payload plus the raw schema text, no data. That would put a
   182-model schema behind the DDL gate permanently, which is the only way this
   stops being reactive.
3. Failing that, a periodic re-run at their convenience.

---

## Still open, unchanged

- **PostgreSQL only.**
- **Writes and transactions are on the STOP list.** Their measurement was 62% of
  call sites. Now that Phase 3 should complete, this is the thing standing
  between them and Phase 4.
