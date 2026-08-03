# Adoption verification — `5496ec5` / `prisma.sa` 1.2.0

You asked for the re-run. Here it is, same 182-model production schema, same
cluster, same procedure.

**The diff is zero.**

| | before | 1.1.0 | **1.2.0 (measured)** |
| --- | ---: | ---: | ---: |
| `pg_dump` diff, `db push` vs `create_all` | 2,191 lines | 56 | **0** |
| Alembic baseline (`upgrade()`) | 5,766 lines | 78 | **empty — `pass`** |
| Shims needed to reach Phase 3 | 3 | 1 | **0** |

Your "expected" column was right on our schema too. B3's remainder, B6 and B7
are all confirmed fixed against a live database.

---

## What was verified

```console
$ python -c "from prisma import sa; print(sa.__version__); print(len(sa.metadata().tables))"
1.2.0
182

$ # create_all() into an empty database — no monkeypatching of any kind
create_all(): OK — NO SHIMS
```

| | Result |
| --- | --- |
| **B3** identifiers > 63 chars | **0** (was 7 after 1.1.0, 10 originally) |
| **B6** `sort: Desc` | all 12 emitted with `DESC`, including the 11 composite |
| **B7** `@relation(map:)` | `bi_public_share_resource_id_fkey` — honoured |
| `pg_dump --schema-only` diff | **0 lines** |
| Alembic `upgrade()` | `pass` |
| Backend test suite | 1 failed / 1,378 passed, 1,412 passed — identical to baseline throughout |

This is the first time `create_all()` has run on this schema without a shim, and
the first time the two dumps have been byte-identical. Tables, columns, types,
precisions, nullability, column order, enum labels, sequences, FK actions, all
495 index names *and their sort directions*, and every constraint name now agree.

The B3 fix in particular is the right shape — moving the constraint name into
the metadata where the truncation rule already lived, rather than special-casing
foreign keys in the builder, is why it is now structurally hard for the next
constraint type to miss it.

---

## One follow-up: the strict gate does not go empty

Narrow, cosmetic in the database, but it lands exactly on the advice D6 added.

The runbook now tells adopters to enable `compare_server_default=True`. With it
on, the baseline is **not** empty — 16 `alter_column` operations, one per
`autoincrement()` column:

```py
op.alter_column('attachments', 'attachment_number',
           existing_type=sa.INTEGER(),
           server_default=sa.text('nextval(\'"attachments_attachment_number_seq"\'::regclass)'),
           existing_nullable=False,
           autoincrement=True)
```

These are **false positives** — the `pg_dump` diff is zero, so the databases
genuinely agree. The cause is quoting:

```
metadata server_default : nextval('"attachments_attachment_number_seq"'::regclass)
stored in PostgreSQL    : nextval('attachments_attachment_number_seq'::regclass)
```

PostgreSQL normalises the quoted identifier away on storage, because the name
needs no quoting. Alembic's `compare_server_default` is a text comparison, so it
sees a difference that will never converge.

Confirmed by testing the hypothesis directly — stripping the quotes from the
metadata's `server_default` and re-running autogenerate drops it from 16
operations to 1–2:

| metadata form | strict-gate operations |
| --- | --- |
| `nextval('"name"'::regclass)` (current) | 16 |
| `nextval('name'::regclass)` | 1–2 |

Emitting the identifier unquoted — or quoting only when the name actually
requires it — would close this. The DDL is equivalent either way; only the text
comparison cares.

### A caveat on D6 worth documenting

The 1–2 that survive unquoting are **not** a library defect, and they are
**non-deterministic** — three consecutive runs gave:

```
run 1: 1 op  -> pods
run 2: 2 ops -> documents, pods
run 3: 2 ops -> instructions, pods
```

That is Alembic's own SERIAL-detection heuristic (`Detected sequence named
'…_seq' as owned by integer column '…', assuming SERIAL and omitting`) firing
inconsistently across reflection runs.

Since the runbook's rule is "not empty → STOP", it is worth a line in §3 saying
that with `compare_server_default=True` a small, varying number of
sequence-backed columns may appear, and that the `pg_dump` comparison — not
autogenerate — is the authority when the two disagree. Otherwise an adopter
following the strict advice stops on noise.

**With the default `compare_server_default=False`, the gate is cleanly empty.**

---

## On the fixture offer

Both items you asked for, answered honestly.

**1. The re-run — done, and standing.** We are happy to keep doing this. It costs
about ten minutes and the harness stays built.

**2. The recorded DMMF fixture — we need to ask before handing it over.** The
payload is schema shape only, no rows, but a 182-model datamodel is a fairly
complete description of the business, and it would live permanently in a public
repository. That is our client's call, not ours, and we have put the question to
them rather than answering on their behalf. We will come back either way.

If the answer is no, option 3 — a periodic re-run on request — remains open and
we would suggest treating it as the standing arrangement. It gives you most of
the signal, and it has now caught eight defects across three rounds that the
synthetic fixture did not contain.

---

## Still open, unchanged

Confirmed as unchanged from your list, none of them regressions:

- **PostgreSQL only.**
- **`relationMode = "prisma"`** — not applicable to us (we do not use it), so we
  cannot verify a fix here either way.
- **Writes and transactions on the STOP list** — still 62% of our call sites,
  and now genuinely the only thing between us and Phase 4. Phase 3 completes.

For what it is worth, from the consuming side: with the DDL gate passing, the
Alembic handover is now something we would actually be willing to run. That was
not true three rounds ago.
