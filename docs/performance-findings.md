# Performance findings

What we measured, what worked, what didn't, and what to do about it.

Everything here was measured on one rig — a 41-model publishing-platform schema
(self-relations, N-M joins, enums, `Json`/`Bytes`/`Decimal`), a real PostgreSQL
16, and **both** a sync and an asyncio client generated from it and used in the
same process. The harnesses live in
[`benchmarks/pg-lab/`](https://github.com/oscarnevarezleal/prisma-client-py/tree/develop/benchmarks/pg-lab)
and every number below is reproducible from there.

---

## The headline

| | baseline | best | change |
| --- | ---: | ---: | ---: |
| Retained RSS (both clients live) | 337.6 MB | **59.2 MB** | **−82%** |
| Import time (both clients) | 4,208 ms | **366 ms** | **−91%** |
| Query latency (10 shapes) | 27.96 ms | 27.91 ms | **unchanged** |

`baseline` is what an upstream user gets today: default generator options, two
separately generated packages for sync + async.

**The memory and startup wins are large and real. Query latency was never
winnable from Python** — see [Where the time goes](#where-the-time-actually-goes).

---

## What to actually turn on

Most of the win is unglamorous. In descending order of value:

```prisma
generator client {
  provider             = "prisma-client-py"
  recursive_type_depth = -1      // true-recursive types
  minimalRuntime       = true    // query-arg types -> .pyi stubs
  separateModelFiles   = true    // lazy per-model files
  lazyActions          = true    // action namespaces built on first access
}
```

That group alone takes 337.6 MB → 66.9 MB. Everything else in this fork adds a
further ~8 MB (~12%) on top of it.

Then, if you need them:

| | when it helps | measured |
| --- | --- | --- |
| `modelBackend = "msgspec"` or `"slim"` | you can trade validation-of-arbitrary-input for speed/memory | −8 MB, ~tied with each other |
| unified sync+async package | you use both interfaces in one process | −42 MB at 20 models, −218 MB at 50 |
| `PRISMA_PY_SHARED_ENGINE=1` | both clients connected **simultaneously** | 2 engine processes / 48 MB → 1 / 24 MB |
| `PRISMA_PY_RAW_DECODE=1` | you read **large result sets** (200+ rows) | −7 to −14% |
| `query_raw` on hot paths | latency matters more than the query API | −28% (1 row) to −46% (400 rows) |

---

## Where the time actually goes

This is the finding that reframed the whole effort. Timing the same query at
three levels (`where_time_goes.py`):

| attribution | 1 row | 400 rows |
| --- | ---: | ---: |
| postgres itself (psycopg) | 0.17 ms (**9%**) | 0.90 ms (**12%**) |
| engine transport + execution | 1.23 ms (63%) | 3.18 ms (42%) |
| GraphQL planning + serialization | 0.55 ms (28%) | 3.48 ms (46%) |
| **total (full ORM path)** | 1.96 ms | 7.56 ms |

**The database is ~10% of a Prisma query. The query engine is ~90%.** The same
query is 8–12× slower through Prisma than through psycopg.

Ruled out as explanations — all measured, all working correctly:

- postgres connection churn (engine pools; 50 queries, one backend, unchanged
  `backend_start`)
- HTTP connection churn (httpx keep-alive; 31 requests on one connection)
- concurrency serialization (throughput *improves* under load: 2.5 ms/query
  sequential → ~1.6 ms at 80+ concurrent)

The overhead is intrinsic to the binary query engine: a subprocess hop plus
GraphQL parse/plan/serialize per call. **No Python-side change can move it** —
eliminating *all* client deserialization would still leave ~88% untouched.

---

## Negative results

These are kept deliberately. They cost real time to discover and are cheaper to
read than to re-derive.

**`PRISMA_PY_FAST_PARSE` — skipping validation is not faster.** Building
Pydantic instances via `model_construct` after a compiled conversion plan lost
to plain `model_validate` twice (+12.6%, then +0.6% after warming the plans).
pydantic-core's Rust validator beats a Python-side construction loop. The
mechanism ships **disabled**; "skip validation for speed" is a myth on
Pydantic v2.

**`scalarFieldsOnly` breaks `include`.** Dropping relation fields from models
raises `UnknownRelationalFieldError` the moment a query uses `include=`. The
loop auto-rejected it by crashing, which is the correct outcome — an
optimization that changes behavior must prove the app doesn't depend on that
behavior.

**`recursiveValidationModels` is the right tool for a different patient.** It
wins when eager model building makes import slow or impossible (100+ chained
models). Here RSS was already floored, so `defer_build` only moved cost from
import into first-query: 0.93% *worse*.

**The one-pass decode was oversold before it was profiled.** In isolation
msgspec decodes ~6× faster than `json.loads` + build; that looked like a
structural win. Profiling first showed deserialization is only ~5% of a small
query, so the honest ceiling was ~5%, not 6×. It was built anyway — correctly
scoped as a bulk-read optimization — and small responses were measurably
*slower*, so the path is now gated on response size.

---

## Methodology notes (read before trusting any A/B here)

**Sequential A/B measurement on a shared machine is not trustworthy at this
resolution.** Two back-to-back runs of the same comparison disagreed by 13
percentage points. Every close call in this document therefore uses interleaved
passes (A,B,A,B,… in fresh processes, alternating direction), which is what
`head2head.py` and `rawdecode_bench.py` do. Two conclusions were nearly shipped
wrong because of this:

- `slim` was first rejected at −0.06%, then accepted at −2.9% after its
  deserializer was rewritten — but the ordering vs `msgspec` has since flipped
  between runs. **They are statistically tied**; choose on constraints
  (msgspec = maintained C library, faster on bulk; slim = no dependency,
  marginally lower RSS), not on the composite.
- The ladder loop mis-ranked backends under CPU contention, which is why the
  scorecard was re-measured in a single interleaved run rather than composed
  from earlier ones.

**A composite score hides workload mismatch.** `PRISMA_PY_SHARED_ENGINE` and
`PRISMA_PY_RAW_DECODE` both score as noise on the standard workload — not
because they don't work, but because it connects clients sequentially and reads
25 rows, while they target simultaneous clients and bulk reads respectively.
Both were verified with targeted measurements instead.

**The loop found a real bug.** `lazyActions` was auto-rejected on its first pass
with a forward-reference crash: eager client construction had been masking a
model-rebuild ordering bug by touching every model as a side effect. Fixed in
`models/__init__` (rebuild until the cache is stable), it became an accepted
−2.5%.

---

## If you are deciding whether to migrate to SQLAlchemy

The 90% engine overhead is the strongest performance argument for leaving, and
no amount of client-side work substitutes for it — a driver-level stack removes
the 90% rather than optimizing the 10%.

But it cuts both ways: **2 ms per query is irrelevant to most request paths**,
and you would be trading away schema-as-source-of-truth, typed query arguments
and Prisma's migration tooling to recover it. Check whether your latency
actually lives in queries first, and note that `query_raw` already reclaims
28–46% of it while keeping typed records.

See [`migrating-to-sqlalchemy.md`](migrating-to-sqlalchemy.md) for the route if
you decide to go.

---

## Reproducing everything

```bash
benchmarks/pg-lab/lab.sh start
export BENCH_DATABASE_URL='postgresql://bench:bench@127.0.0.1:5433/bench'
# ... schema + db push + seed (see benchmarks/pg-lab/README.md) ...

python benchmarks/pg-lab/optimize_loop.py  --workdir . --repeats 3   # the autonomous ladder
python benchmarks/pg-lab/head2head.py      --workdir . --repeats 5   # scorecard vs baseline
python benchmarks/pg-lab/rawdecode_bench.py --workdir . --passes 9   # raw-decode A/B
python benchmarks/pg-lab/where_time_goes.py --workdir .              # db vs engine attribution
```

Raw data for every table above is committed alongside the harnesses as
`benchmarks/pg-lab/results-*.json`.
