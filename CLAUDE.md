# Working in this repository

A fork of `prisma-client-py` carrying two bodies of work on top of upstream:
opt-in generator/runtime optimizations, and `prisma.sa` — the SQLAlchemy
migration toolkit. Both report `prisma.__version__ == '0.15.0'`, same as
upstream, so `prisma.sa.__version__` is the only way to tell which set of fixes
a checkout carries. Bump it whenever the emitted schema changes.

## Running the checks

Run these from the repository root. They are what CI runs; anything else is a
different check.

```bash
PYTEST_PLUGINS=pytester python -m pytest tests/ -q     # ~50 tests error at setup without it
python -m ruff check . && python -m ruff format --check .
python -m mypy --namespace-packages --package prisma --package tests
python -m pyright src tests
python -m pyright --ignoreexternal --verifytypes prisma
```

The live-database half of `tests/test_sqlalchemy` needs a PostgreSQL:

```bash
export PRISMA_SA_TEST_DATABASE_URL=postgresql://user:pass@127.0.0.1:5432/db
```

Without it those tests skip — which is not a neutral outcome, see the coverage
gate below.

### Traps in the commands themselves

- **`ruff check .` and `ruff check <path>` are not the same.** A named path
  bypasses the `exclude` config, so checking `src/` reports ~260 errors in
  generated files that CI never sees. Always pass `.`.
- **`pyright src tests` and `pyright --verifytypes` are separate gates**, both
  in the `lint` session. `verifytypes` inspects the *public* interface only, so
  exporting a previously-internal symbol can turn it red while the plain run
  stays green. Anything reachable from `prisma.sa.__all__` needs declared types,
  not inferred ones.
- **Never `rm -f .coverage*`** — the glob eats `.coveragerc`. Use
  `rm -f .coverage .coverage.*`.
- The suite serialises against one database. Two pytest processes sharing a
  `PRISMA_SA_TEST_DATABASE_URL` fight over the scratch databases derived from it.

### The coverage gate is stricter than it looks

`nox -s report-strict` requires **100 % coverage of `tests/**` itself**, not of
`src`. Consequences that are easy to miss:

- A test that *skips* has an uncovered body and fails the gate. Skipping is not
  a safe default here — either make the dependency available, or exclude the
  test by name in `.coveragerc`.
- `any(...)` and `next(x for x in ...)` leave the generator's loop-exit arc
  unexecuted and register as partial branches. Use a comprehension plus an
  assertion; it also produces a better error than a bare `StopIteration`.
- Coverage is *combined across CI jobs*, so a line executed in any one job
  counts. One job running the live-database tests satisfies the gate for all.
- Entries in `.coveragerc`'s `exclude_lines` are **regexes, not comments**. A
  `#` line inside that list becomes a pattern. Put prose above the section.

## Compatibility floors that will bite you

The test matrix runs **Python 3.8 through 3.12**, on Linux and Windows, against
**both Pydantic v1 and v2**. Your development environment is almost certainly
more permissive than all of them.

- **Check `requires_python` on PyPI before pinning any dependency.** This has
  broken the matrix three times: `msgspec` 0.21 (needs ≥3.10), `psycopg` 3.3
  (≥3.9), `alembic` 1.16 (≥3.9). Where a single pin cannot span the range, split
  it by marker — see `pipelines/requirements/deps/msgspec.txt`.
- Anything a test shells out to must be in `pipelines/requirements/test.txt`, or
  it works locally and fails in CI. `ruff`, `mypy`, `alembic` and `psycopg` are
  all there for exactly that reason.
- Pydantic v1 spells things differently: `construct` not `model_construct`,
  `parse_obj` not `model_validate`. Go through `prisma._compat` rather than
  calling either directly.
- A dependency can also break *without* a version bump on our side: `argcomplete`
  3.7.1 (nox's own, previously unpinned) uses PEP 604 syntax at class-definition
  time and takes `import nox` down on 3.9, while still advertising `>=3.8`.

## `prisma.sa` — what holds it together

**The DDL-equivalence gate is the strongest test in the repository.**
`tests/test_sqlalchemy/test_ddl_equivalence.py` builds one database with
`prisma db push` and another with `MetaData.create_all()`, and requires
`pg_dump --schema-only` to be *identical*. That is strictly stronger than an
empty Alembic autogenerate diff, which ignores foreign-key actions, constraint
names, index methods, column order, server defaults and sequences.

**`build_metadata()` is the single source of DDL truth.** `_declarative.py`
reads every DDL-affecting decision off the `MetaData` it produced and never
recomputes one. Keep it that way: a declarative layer that drifts from the Core
layer is worse than none, because it looks authoritative.

**Four annotations live in the Prisma schema language but never reach the
DMMF**, and are recovered by lexing the raw schema text in
`generator/_native_types.py`: `@db.*` native types, `@relation(map:)`,
`@relation(onUpdate:)` — and `relationMode`, which is *not yet* wired into the
builder (see below). If a fifth turns up, it goes through the same shared,
string-aware `@relation(` scanner rather than a new regex.

**The runbook is the contract.** `docs/prisma-to-sqlalchemy-runbook.md` §4.1
lists translations that were executed against a live database and compared row
for row; §5 lists those with no verified translation. `prisma py sqlalchemy
doctor` classifies user code against exactly those two lists and reports
anything in neither as `unknown`. Do not add a §4.1 row you have not run.

## The lesson this project keeps relearning

> A fixture only tests what it contains.

Four rounds of silent DDL divergence have had the identical shape: an annotation
absent from the reference schema, so the `pg_dump` gate passed on a hardcoded
constant. Three were reported by a client running against a 182-model production
schema; the fourth we found ourselves.

Two habits follow, and they are worth more than any individual fix:

1. **Verify the gate bites.** Restore the bug and watch the test fail. A test
   that would pass either way proves nothing — several written here originally
   did, including a three-valued-logic case where every row was NULL so the
   correct and incorrect SQL agreed.
2. **Extend the fixture with the shape, not just the case.** The reported bug was
   a long `_key`; the `_fkey` path with the same root cause was untouched and
   failed the next round.

## Measured facts worth not re-deriving

- Prisma's `every` uses naive `NOT (C)`, not `IS NOT TRUE` — measured, and the
  opposite of what the design doc originally claimed.
- Identifier truncation keeps the suffix and cuts the base:
  `base[:63-len(suffix)] + suffix`.
- Prisma fills `cuid()`, `uuid()`, `nanoid()`, `now()` and `@updatedAt`
  **client-side**, not in the database. A bare `INSERT` that skips
  `values_for_create` writes a row Prisma would not have written.
- `sa.insert(t).values([...])` compiles its `VALUES` clause from the **first**
  mapping and silently drops keys only later rows carry. Group by key set.
- `rowcount` is `-1` for INSERT under psycopg; take counts from `RETURNING`.
- `db.tx()` is `engine.begin()`, at `read committed` on both sides. A nested
  `tx()` is a second independent transaction, *not* a savepoint — `begin_nested()`
  is the wrong translation.
- A write's return value is the same row but not the same Python objects as a
  `RETURNING` row: Prisma gives the enum member and an aware datetime, `RETURNING`
  gives the stored label and a naive one.
- Query engine → SQLAlchemy on a mixed workload: 8.49 ms → 3.48 ms total,
  connect 54–72 ms → 6–8 ms. Memory is a wash — removing the 24.7 MB engine
  subprocess is offset by importing SQLAlchemy.

## Open, for subsequent rounds

Roughly in order of how much damage each can do.

- **`relationMode = "prisma"` is not wired into the builder.** Same family as the
  four above and worse in kind: the database then has *no* foreign keys, so every
  FK `prisma.sa` builds diffs against every table. `_doctor.py` already lexes it
  for the scope check; nothing feeds it into `build_metadata`.
- **`Json` decoding is broken on the msgspec backend.** `_dec_hook` returns the
  decoded object, but msgspec requires the hook to return an instance of the
  annotated type, so any *populated* `Json` column raises
  `msgspec.ValidationError`. `NULL` is fine. Fixing it means changing the shared
  record annotations.
- **`modelBackend = "slim"` has no partial-type surface.** The msgspec hole was
  closed; the slim branch of `models/_model.py.jinja` still emits no
  `create_partial`.
- **A `lazyActions` read-path bug.** `find_unique(...).title` followed by
  `find_many(include={...})` raises `FieldNotFoundError`; generating the same
  schema without `lazyActions` passes. Looks like a stale field list leaking
  between lazily-built model namespaces.
- **Required list scalars get no `None → []` coercion on msgspec**, because that
  validator only exists in the pydantic branch.
- **Writes and transactions still gate a real migration.** A client measured 62 %
  of their call sites on the STOP list. §4.1 now covers flat writes, bulk writes,
  upsert and transactions; nested writes, `connect`/`disconnect`/`set`, atomic
  operations and `tx(timeout=)` remain.
- **`aggregate()` is on §5 but this client never generates it**, so that row can
  never fire here. Kept for codebases migrating *onto* this fork; the reachable
  form is `group_by(sum=)`.
- **`docker (linux/amd64, slim-bullseye)` fails intermittently** fetching Node
  from nodejs.org inside `nodeenv`. Not ours — the arm64 job builds the same
  Dockerfile and passes. A re-run clears it.

## Conventions

- Commit messages explain *why*, and say plainly what was measured versus
  assumed. If something could not be verified in this environment — a Python 3.8
  failure, a live re-run — say so rather than implying it was checked.
- Do not weaken a test to make a change pass.
- Refuse rather than approximate. Every emitter here raises or omits when a shape
  cannot be expressed faithfully, and records the reason; a silently wrong model
  is the worst outcome, because it looks authoritative.
