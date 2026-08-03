# Adoption retest — same schema, library commit `22f9337`

> **Status:** the three items below were fixed in `5496ec5` (`prisma.sa` 1.2.0).
> We re-ran again — the `pg_dump` diff is now **zero** and the Alembic baseline
> is **empty**. See [`03-adoption-verification-kompas.md`](03-adoption-verification-kompas.md).

Re-ran the full runbook against the same 182-model production schema that
produced [the original field report](01-adoption-report-kompas.md), on the same
PostgreSQL 16 cluster, with the same procedure.

**B1, B2, B4 and B5 are fixed — confirmed against a live database, not just
against your tests.** B3 is half-fixed. Two new defects surfaced that the
original report could not reach, because `sa.metadata()` crashed before it got
there.

| Measure | Before | After |
| --- | --- | --- |
| `pg_dump` diff (`db push` vs `create_all`) | 2,191 lines | **56 lines** |
| Alembic baseline vs datamodel-exact DB | 5,766 lines, 1,106 `alter_column` | **78 lines, 12 index recreations** |
| Shims needed to reach Phase 3 | 3 (B2, B3, B4) | 1 (B3 remainder) |
| Backend test suite | identical to baseline | identical to baseline |

---

## Confirmed fixed

Phase 2's verification command now succeeds with **no shims**:

```console
$ python -c "from prisma import sa; print(sa.metadata() is not None, sa.table_for('Journey').name)"
True journey
```

| | Evidence |
| --- | --- |
| **B2** `uuid(4)` | 182/182 tables built; no `NotImplementedError` |
| **B1** `@db.*` | `Journey.id` is now `UUID()`, was `Text()`. **531 `uuid` columns on both sides** — exact match. `@db.VarChar(n)` likewise. |
| **B5** sequences | **16 on both sides.** `attachments.attachment_number` default is `nextval('attachments_attachment_number_seq'::regclass)` in both. |
| **B4** empty array | `create_all()` completes; `ARRAY[]::uuid[]` matches. |
| **D4** `join_ambiguous` | present on **640/640** relations |
| **D7** version | `prisma.sa.__version__ == '1.1.0'` while `prisma.__version__ == '0.15.0'` |

All three parts of the B5 fix verified in the dump — `AS integer` (16 sequences
on each side), the `nextval` server default, and `ALTER SEQUENCE … OWNED BY`.
The `OWNED BY` detail in particular is the kind of thing that would have been
easy to skip; it is present and correct.

The drop-in guarantee also held: 1 failed / 1,378 passed (API) and 1,412 passed
(shared), byte-identical to the pre-migration baseline, so none of the type
changes leaked into client behaviour.

---

## B3 — half fixed

The unique-constraint case from the report is fixed. **Foreign key names are
not truncated**, so `create_all()` still raises:

```console
sqlalchemy.exc.IdentifierError: Identifier
'journey_order_instruction_geofences_journey_order_instruction_id_fkey'
exceeds maximum length of 63 characters
```

3 of the original 10 are fixed. The 7 that remain are **all
`ForeignKeyConstraint`**:

| Length | Name |
| --- | --- |
| 81 | `journey_order_instruction_evidence_log_journey_order_instruction_evidence_id_fkey` |
| 77 | `journey_order_instruction_issue_attachments_journey_order_instruction_id_fkey` |
| 71 | `journey_order_instruction_attachments_journey_order_instruction_id_fkey` |
| 69 | `journey_order_instruction_issue_attachments_instruction_issue_id_fkey` |
| 69 | `journey_order_instruction_geofences_journey_order_instruction_id_fkey` |
| 68 | `journey_order_instruction_evidence_journey_order_instruction_id_fkey` |
| 65 | `journey_order_instruction_issue_journey_order_instruction_id_fkey` |

Prisma truncates these identically to the unique case — keep the suffix, cut the
prefix to fit 63:

```
journey_order_instruction_geofences_journey_order_instruct_fkey    (63)
```

This is the *"fixture only tests what it contains"* lesson recurring inside its
own fix. The report's example was a `_key`; it became the test case; the `_fkey`
path was never exercised. Worth adding one of the above alongside it.

---

## B6 (new) — `sort: Desc` on an index is silently dropped

Not reachable before, because B2 crashed first. 12 index declarations carry
`sort: Desc`; all 12 are emitted ascending.

```
prisma db push :  CREATE INDEX idx_journey_org_created ON journey USING btree (organization_id, created_at DESC)
create_all     :  CREATE INDEX idx_journey_org_created ON journey USING btree (organization_id, created_at)
```

From `@@index([organizationId, createdAt(sort: Desc)], map: "idx_journey_org_created")`.

**11 of the 12 are composite, and that is what makes this more than cosmetic.**
For a single-column index, `DESC` is close to free — PostgreSQL scans backwards.
For a composite index it is not: `(a ASC, b ASC)` scanned backwards yields
`(a DESC, b DESC)`, which serves neither `ORDER BY a, b DESC` nor
`ORDER BY a DESC, b`. The index the schema asked for cannot be substituted by
the one that gets built, so these queries fall back to a sort node.

Every one of the 11 is a `(scope_column, timestamp DESC)` pair — the shape used
for "latest N for this org/customer/journey" paging. Silent to the application,
visible only as a slow query later.

Good news: Alembic *does* catch this one, so it is not a repeat of B5:

```
Detected changed index 'idx_journey_org_created' on 'journey':
  expression #2 'created_at DESC' to 'created_at'
```

Those 12 recreations are the **entire** remaining content of the Alembic
baseline. Fix B6 and the gate goes empty.

---

## B7 (new) — `@relation(map: "…")` is ignored

A custom foreign key constraint name is discarded and the name is derived from
the table instead:

```prisma
model BiPublicShare {
  resource BiResource @relation(fields: [resourceId], references: [id],
                                onDelete: Cascade,
                                map: "bi_public_share_resource_id_fkey")
  @@map("bi_public_shares")
}
```

```
prisma db push :  bi_public_share_resource_id_fkey     ← honours map:
create_all     :  bi_public_shares_resource_id_fkey    ← derived from @@map
```

Our schema has 3 uses of `@relation(map:)`; only this one surfaces, because the
other two happen to coincide with the derived name. That is the trap — the bug
is invisible except when `map:` is doing something, which is the only reason to
write it.

Note the contrast: `@@index(map:)` **is** honoured correctly — all 154 of ours
match, and index names came out 495/495 exact. It is specifically the relation
form that is dropped.

Alembic does not compare constraint names, so this one is silent to the gate —
same class as B5 was.

---

## Where that leaves the diff

Remaining 56 lines of `pg_dump` difference, in full:

| Cause | Lines |
| --- | --- |
| B6 — `DESC` dropped from 12 indexes | 52 |
| B7 — one FK constraint name | 4 |

Nothing else. Tables, columns, types, precisions, nullability, column order,
enum types and labels, FK referential actions, sequences and all 495 index names
agree exactly. With B3's remainder, B6 and B7 closed, this schema would produce
the identical dumps the docs claim — and an empty Alembic baseline, which is the
Phase 3 gate.

---

## On the two structural lessons

Both landed, and the retest is evidence for the first one rather than against
it: B6 and B7 are two more shapes the reference schema does not contain
(`sort: Desc`, `@relation(map:)`), found the same way as the original five — by
running a real schema against a real database. Neither is exotic; both are
ordinary Prisma. Adding them, plus a long `_fkey`, would close the loop.

The second lesson — *"identical dumps was a claim about one schema"* — is the
right framing, and the retest is a decent argument for treating an external
schema as a recurring test input rather than a one-off report. We are happy to
re-run this on request against any future commit; the harness is standing and
takes about ten minutes.

---

## Reproduction

Same environment as the original report. The three findings above reproduce from
a schema containing:

```prisma
model Thing {
  id        String   @id @default(uuid()) @db.Uuid
  scopeId   String   @db.Uuid
  createdAt DateTime @default(now())
  other     Other    @relation(fields: [scopeId], references: [id], map: "custom_fk_name")

  @@index([scopeId, createdAt(sort: Desc)])   // B6 — DESC dropped
  @@map("things")                             // B7 — map: ignored, derived from here
}
```

plus any relation whose derived `<table>_<column>_fkey` exceeds 63 characters
for the B3 remainder.
