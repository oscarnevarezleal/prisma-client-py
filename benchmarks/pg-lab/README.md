# pg-lab: dual-client optimization loop against live PostgreSQL

The other harnesses in `benchmarks/` measure generated-code size and import cost
in isolation. This one closes the loop against a **real database**: a realistic
41-model schema (publishing platform: orgs/teams/sites/posts/revisions/comments/
tags/media/billing/webhooks/audit, with self-relations, N-M join tables, enums,
`Json`/`Bytes`/`Decimal` columns), both `interface = "sync"` and
`interface = "asyncio"` clients generated from it, connected simultaneously to
PostgreSQL, running a mixed query workload.

`optimize_loop.py` then walks a ladder of candidate optimizations autonomously,
accepting each only if it improves a composite cost (memory x import x connect x
query latency) by >= 0.5% without regressing query latency, and stops at
diminishing returns.

## Running

```bash
# 1. database (docker compose preferred; falls back to system postgres 16)
benchmarks/pg-lab/lab.sh start
export BENCH_DATABASE_URL='postgresql://bench:bench@127.0.0.1:5433/bench'

# 2. schema + DDL + seed
mkdir -p /tmp/pglab && cd /tmp/pglab
python $REPO/benchmarks/pg-lab/schema_gen.py --interface asyncio \
    --output ./pkg_async --schema ./async.prisma
python -m prisma db push --schema=./async.prisma --skip-generate
psql "$BENCH_DATABASE_URL" -f $REPO/benchmarks/pg-lab/seed.sql

# 3. the loop
python $REPO/benchmarks/pg-lab/optimize_loop.py --workdir . --repeats 3 \
    --json loop_results.json

# 4. phase 2, starting from whatever phase 1 actually settled on
python $REPO/benchmarks/pg-lab/optimize_loop.py --workdir . --repeats 3 \
    --phase 2 --phase1-json loop_results.json --json loop_phase2.json
```

Each loop iteration emits one JSON line (`ACCEPT`/`REJECT` + scores); the run
ends with a `DONE` summary and regenerates the winning configuration in the
workdir.

## The candidate ladder

Applied cumulatively, in expected-value order; a candidate that fails to
generate, crashes the workload, or produces output that is not a full set of
metrics is auto-rejected (that's the filter that catches behavior-changing
options like `scalarFieldsOnly` under an `include=`-heavy workload):

1. `recursive_type_depth = -1` — true-recursive types
2. `minimalRuntime = true` — query-arg types into `.pyi` stubs
3. unified package — sync+async share `models.py`/`types.py`/`bases.py`
   (the graft prototyped in `benchmarks/dual_interface.py`)
4. `separateModelFiles = true` — lazy per-model files
5. `recursiveValidationModels = true` — `defer_build`, validators on first use
6. `scalarFieldsOnly = true` — drops relation fields (expected: REJECT)

## Results

Run of 2026-08-03 (Python 3.11, PG 16, 41-model schema, medians of 3 fresh
processes x 20 query rounds; raw data in `results-2026-08-03.json`):

| # | candidate | verdict | RSS (MB) | import (ms) | connect (ms) | queries (ms) | composite |
| - | --- | --- | ---: | ---: | ---: | ---: | ---: |
| 0 | baseline (`depth=5`, two packages) | — | 334.8 | 3,911 | 167 | 29.1 | 282.4 |
| 1 | `recursive_type_depth = -1` | **ACCEPT −39.9%** | 142.3 | 1,359 | 152 | 28.3 | 169.7 |
| 2 | + `minimalRuntime` | **ACCEPT −30.3%** | 78.8 | 653 | 141 | 26.9 | 118.2 |
| 3 | + unified package | **ACCEPT −15.0%** | 64.8 | 425 | 137 | 26.9 | 100.5 |
| 4 | + `separateModelFiles` | **ACCEPT −6.1%** | 64.1 | 315 | 146 | 27.0 | 94.4 |
| 5 | + `recursiveValidationModels` | REJECT (−0.93%, worse) | 64.0 | 348 | 136 | 27.2 | 95.3 |
| 6 | + `scalarFieldsOnly` | REJECT (workload crash) | — | — | — | — | — |

**End state: 334.8 → 64.1 MB (−81%) and 3.9 s → 0.32 s import (−92%), with
query latency flat** (29.1 → 27.0 ms across ten shapes — generator options
don't touch the engine, and the loop verified that instead of assuming it).

Reading the rejections:

- `recursiveValidationModels` **helps a different regime.** Its win is when
  eager model building makes import slow or impossible (100+ chained models);
  here RSS was already floored by minimalRuntime+unified, so `defer_build`
  only moved cost from import into first-query and the composite got 0.93%
  worse. Right tool, wrong patient.
- `scalarFieldsOnly` was rejected **by the workload, not by the score**:
  `include={'author': ...}` raises `UnknownRelationalFieldError` once relation
  fields are gone from the models. That is the auto-reject filter doing its
  job — an optimization that changes behavior has to prove the app doesn't
  depend on that behavior, and this app does.

Phase-1 conclusion: with the current generator, ~64 MB / ~315 ms is the floor
for this schema — everything past iteration 4 is <0.5% or behavior-breaking.
The remaining big-ticket items are structural, not configuration: making
`interface = "both"` a real generator mode (the graft is still a build hack;
see `docs/contributing/unified-sync-async-client.md`), stub-splitting
`actions.py`'s docstrings, and the ~22 MB/process query-engine sidecars.

## Phase 2: foundational mechanisms (`--phase 2`)

Phase 1 exhausted the existing generator options. Phase 2 walks a ladder of
mechanisms implemented in the fork for this purpose (none exist upstream), on
top of the configuration a phase-1 run settled on. That baseline is read out of
the phase-1 result file (`--phase1-json`, required with `--phase 2`) rather
than restated in the script, so changing the ladder, the threshold or the
guardrail cannot leave phase 2 measuring against a winner no run ever picked.

The ladder, in order:

| # | mechanism | switch | what it does |
| - | --- | --- | --- |
| 1 | lazy actions | `lazyActions = true` | action namespaces + the actions module import deferred to first DB access |
| 2 | shared engine | `PRISMA_PY_SHARED_ENGINE=1` | sync + async clients attach to one refcounted query-engine process |
| 3 | fast parse | `PRISMA_PY_FAST_PARSE=1` | trusted engine responses skip validation (compiled converters + `model_construct`) |
| 4 | slim models | `modelBackend = "slim"` | pydantic-free `__slots__` records deserialized by codegen-unrolled converters |
| 5 | msgspec models | `modelBackend = "msgspec"` (+ `separateModelFiles = false`) | C-decoded `msgspec.Struct` records |

Run of 2026-08-03, v2 (after fixes below; raw data `results-phase2-2026-08-03.json`):

| # | candidate | verdict | RSS (MB) | import (ms) | queries (ms) | composite |
| - | --- | --- | ---: | ---: | ---: | ---: |
| 0 | phase-1 winner (baseline) | — | 63.8 | 345 | 28.2 | 96.1 |
| 1 | `lazyActions` | **ACCEPT −2.5%** | 64.0 | 314 | 27.6 | 93.7 |
| 2 | shared engine | REJECT (±noise, see below) | 64.1 | 319 | 27.4 | 95.3 |
| 3 | fast parse | REJECT (slower) | 64.2 | 305 | 29.9 | 94.2 |
| 4 | `modelBackend = "slim"` | **ACCEPT −2.9%** | **58.3** | 328 | 26.4 | **90.9** |
| 5 | `modelBackend = "msgspec"` | *not in this run — see below* | — | — | — | — |

**That recorded run is four rungs, not five.** The msgspec rung was added to the
ladder afterwards, with the backend itself; nobody has re-run the phase-2 loop
since, so `results-phase2-2026-08-03.json` contains no msgspec row and its
numbers are not a measurement of the current ladder. msgspec-vs-slim was
pursued with `head2head.py` instead of by re-running this loop — with the
caveats recorded in that section. Re-running `--phase 2` will produce a fifth
rung; until someone does, this table is the honest state of it.

Cumulative across both phases: **334.8 → 58.3 MB (−83%) and 3.9 s → 0.33 s.**

What the loop taught us (the rejections are the valuable part):

- **The loop caught a real bug on its first pass.** `lazyActions` was
  auto-rejected in run 1: without the eager "touch every model" side effect,
  transitively loaded models never got their pydantic forward references
  rebuilt and the query builder crashed. The fix (rebuild until the model
  cache is stable in `models/__init__`) turned it into an accepted −2.5%.
- **`fast-parse` lost twice (+12.6%, then +0.6% after warm plans).**
  pydantic-core's Rust validator is *faster* than a Python-side
  `model_construct` loop. "Skip validation for speed" is a myth on Pydantic
  v2 — the mechanism ships disabled, kept as a documented negative result.
- **`slim` went from rejected (−0.06%) to accepted (−2.9%) by codegen.**
  The first version's generic per-field loop cost +5.9% query latency —
  eating its own −13% RSS win. Unrolling `from_engine` into an
  exec-compiled function per model (the namedtuple trick) removed the
  regression: −5.5 MB RSS *and* faster queries than the pydantic backend.
- **The shared engine's win is invisible to the composite, but real.** The
  workload connects the two clients sequentially, so the second engine never
  coexists with the first in the RSS metric. Measured with both clients
  connected simultaneously — the actual dual-client scenario:
  **2 engine processes / 48 MB → 1 process / 24 MB**, verified refcounted
  (first client keeps working after the second disconnects). If your sync and
  async clients are connected at the same time, this flag is worth ~24 MB per
  process regardless of what the composite says.

## Scorecard: where we ended up vs the baseline

One interleaved run of every configuration against the same schema, database
and workload (`head2head.py --set configs --repeats 5 --rounds 25`; raw data in
`results-scorecard-2026-08-03.json`). `baseline` is what an upstream user gets
today: default options, two separately generated packages for sync + async.

Each row is the best known configuration for its backend, so **rows differ from
each other in more than one option** — `config-pydantic` keeps
`separateModelFiles` and no `lazyActions`; `config-msgspec` cannot use
`separateModelFiles` at all. Read them against `baseline`, which is the
question this table answers; for a controlled backend comparison see the next
section.

| variant (`--set configs`) | RSS (MB) | import (ms) | connect (ms) | queries (ms) | composite | vs baseline |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `baseline` | 337.6 | 4208 | 177.0 | 27.96 | 289.6 | — |
| `config-pydantic` (generator options only) | 66.9 | 354 | 147.5 | 27.73 | 99.2 | **−65.7%** |
| `config-slim` | 58.3 | 349 | 146.6 | 26.50 | 94.3 | **−67.4%** |
| `config-msgspec` | 59.2 | 373 | 154.5 | 27.49 | 98.4 | **−66.0%** |
| `config-final` (+ shared engine + raw decode) | 59.2 | 366 | 148.4 | 27.91 | 97.3 | **−66.4%** |

The recorded JSON predates the rename and stores these under `baseline`,
`pydantic`, `slim`, `msgspec`, `final`. The *configurations* are unchanged, so
the numbers still describe these rows; only the labels moved.

**Headline: 337.6 → 59.2 MB (−82%) and 4.2 s → 0.37 s import (−91%), with query
latency unchanged.**

Four things this makes honest that a composite number alone would hide:

- **Most of the win is the generator options**, not the exotic backends.
  `recursive_type_depth = -1` + `minimalRuntime` + `separateModelFiles` +
  the unified package do 337.6 → 66.9 MB on their own. The alternative model
  backends add a further ~8 MB (~12%).
- **`slim` and `msgspec` are effectively tied.** They finish within ~4% of
  each other and the ordering has flipped between runs (msgspec led the
  earlier head-to-head, slim leads this one). Pick on constraints, not on the
  composite: msgspec is faster on bulk deserialization and is a maintained C
  library; slim has no third-party dependency and slightly lower RSS. Note
  that these two rows also differ in `separateModelFiles`, so part of that ~4%
  is not the backend — one more reason not to rank them off this table.
- **Query latency is flat across every variant** (27.96 → 26.5–27.9 ms). That
  is the expected result given that ~90% of a query is above postgres — see
  "Where the time actually goes" below. No client-side change moves it.
- **The two runtime flags look like noise here because this workload does not
  exercise them.** `PRISMA_PY_SHARED_ENGINE` pays off when both clients are
  connected *simultaneously* (measured separately: 48 → 24 MB of engine
  processes); `PRISMA_PY_RAW_DECODE` pays off on large result sets (measured
  separately: −7 to −14% at 200+ rows). The composite workload connects
  sequentially and reads 25 rows, so both are correctly invisible to it.

## Model-backend head-to-head (`head2head.py`)

The ladder measures each candidate once against a moving baseline — right for
exploration, noise-sensitive for close calls (a contended run of the ladder
mis-ranked backends whose true gap is a few percent). `head2head.py` settles
those: it generates each variant once, then interleaves measurement passes
(A,B,C,A,B,C,… rotated by pass index) so machine drift lands on every variant
equally.

`--set backends` holds every non-backend generator option identical across the
three variants, so `modelBackend` is the only thing that differs.
`separateModelFiles` is held **off** for all three: msgspec resolves cyclic
relation references against a single module and cannot use it, and leaving it
on for the other two folds a lazy-per-model-file win into a number labelled
"backend".

**No run of `--set backends` has been recorded yet.** The table below is the
run of 2026-08-03 (7 passes x 30 rounds, raw data
`results-h2h-2026-08-03.json`), and the variants it measured were the *end-state
configs*, not an isolated backend comparison: the `pydantic` variant had no
`lazyActions`, and `separateModelFiles` was on for `pydantic`/`slim` and off
for `msgspec`. Those options move import, memory and the composite, so the
differences below are not attributable to the backend alone.

| end-state config, 2026-08-03 | RSS (MB) | import (ms) | queries (ms) | composite |
| --- | ---: | ---: | ---: | ---: |
| `pydantic` (default backend, no `lazyActions`) | 66.9 | 366 | 29.85 | 100.84 |
| `slim` (+ `lazyActions`, `separateModelFiles`) | **58.4** | **347** | 27.49 | 94.37 |
| `msgspec` (+ `lazyActions`, no `separateModelFiles`) | 59.3 | 353 | **26.29** | **94.07** |

Takeaways:

- **`msgspec` is the recommended alternative backend**, on the numbers above
  plus what is not contaminated by the confound: it has the lowest query
  latency of anything measured here, RSS within 1 MB of slim, and its speed
  comes from a maintained C library instead of slim's bespoke exec-compiled
  deserializers. Query latency is the least affected by the extra options in
  play (`lazyActions` and `separateModelFiles` are import- and memory-side),
  but the composite and import columns should not be read as backend deltas
  until `--set backends` has actually been run.
- In microbenchmarks msgspec converts 5-9x faster than pydantic-core;
  end-to-end that compresses to ~1-3.5 ms/query because the engine round-trip
  dominates — which is also the pointer to the next structural win:
  `msgspec.json.Decoder.decode(raw_bytes)` in the engine HTTP layer would skip
  the `response.json()` dict stage entirely (bytes -> typed structs in one C
  pass, 32 us vs ~210 us for this payload shape).
- msgspec structs resolve cyclic relation references against one module, so
  `modelBackend = "msgspec"` generates a single `models.py` and is
  incompatible with `separateModelFiles`. That trade is cheap: structs
  compile no per-model schemas, so the single module imports flat.
- Pydantic retro-compat is per-record: `model_dump()` / `dict()` /
  `model_dump_json()` / keyword construction / `Model.prisma()` work on
  structs directly, and `to_pydantic()` returns a real `pydantic.BaseModel`
  (lazily created, cached twin class) for integrations that demand one.

## One-pass response decoding (`rawdecode_bench.py`)

`PRISMA_PY_RAW_DECODE=1` (msgspec backend only) decodes the engine's response
bytes straight into record structs, skipping the `bytes -> dict -> records` hop
the client otherwise pays on every query.

**Profile it before you believe the microbenchmark.** In isolation msgspec
decodes this payload shape ~6x faster than `json.loads` + build. End-to-end
that is much smaller, because the engine round-trip dominates:

| stage, `find_many(take=25, include=…)` | time | share |
| --- | ---: | ---: |
| HTTP round-trip (engine plan + DB + serialize + socket) | 3.75 ms | **93.4%** |
| `json.loads(bytes)` -> dict | 0.16 ms | 4.0% |
| dict -> records | 0.10 ms | 2.6% |

Deserialization is only ~5% of a small query — but its share grows with the
result set while the round-trip's fixed cost does not (1 row: 1.4% deser;
400 rows: 17%). So this is a **bulk-read optimization**, not a general one.

Measured A/B, interleaved passes (`rawdecode_bench.py --passes 9`, two
independent runs; raw data in `results-rawdecode-2026-08-03.json`):

| result set | payload | change |
| --- | ---: | ---: |
| 1 row | 0.3 KB | +2 to +5% (below threshold → dict path) |
| 25 rows | 6 KB | −1 to −2% (noise) |
| 25 rows + include | 18 KB | −4 to +1% (noise) |
| 200 rows | 50 KB | **−10%** (both runs) |
| 400 rows | 100 KB | **−8 to −14%** |
| 400 rows + include | 285 KB | **−7 to −13%** |

Because small responses were measurably *slower* (the typed decoder's fixed
dispatch cost exceeds what it saves on a 0.3 KB body), the decode path is gated
on response size: below `PRISMA_PY_RAW_DECODE_MIN_BYTES` (default 20000) the
plain path runs instead. That turns "faster on bulk, slower on single-row" into
"faster on bulk, neutral elsewhere".

Correctness is preserved across the whole surface: relations via `include`,
`Decimal`/`datetime` coercion, `None` results, engine errors (which fall back
to the normal error path), transactions, and `count`/`group_by` — those return
aggregates rather than records, so they are excluded from the fast path by
method name rather than by a failed decode.

**The real frontier is the round-trip, not the client.** At 93% of query time,
nothing in Python can move it; the remaining levers are fewer round-trips
(`batch_()`, fewer N+1 patterns) or a different transport than the HTTP binary
engine.

## Where the time actually goes (`where_time_goes.py`)

Everything above optimizes the Python side. This measures how big that side is,
by timing the same logical query at three levels — bare postgres (psycopg, no
prisma), the same SQL through the client and engine (`query_raw`, no structured
query to plan), and the full ORM path — and attributing the differences.

| level | 1 row | 400 rows |
| --- | ---: | ---: |
| postgres itself (psycopg) | 0.17 ms | 0.90 ms |
| prisma stack, raw SQL (`query_raw`) | 1.40 ms | 4.08 ms |
| prisma stack, full ORM path | 1.96 ms | 7.56 ms |

| attribution | 1 row | 400 rows |
| --- | ---: | ---: |
| postgres itself (psycopg) | 0.17 ms (**9%**) | 0.90 ms (**12%**) |
| client + engine, same SQL | 1.23 ms (63%) | 3.18 ms (42%) |
| structured query on top of that | 0.55 ms (28%) | 3.48 ms (46%) |

**The database is ~10% of a Prisma query. Everything above it is ~90%.** The
same query is 8-12x slower through Prisma than through psycopg.

**Neither difference is query-engine overhead in isolation**, and the script
does not claim otherwise. Row 2 spans the Python client's request handling, the
HTTP round-trip, the engine's connector and execution, and the client's decode
of the reply. Row 3 additionally spans GraphQL parse/plan, the engine's result
serialization *and* record construction — which psycopg never does, since it
returns plain tuples. These are end-to-end differences between two stacks;
splitting the engine process out from the Python client would need
instrumentation inside the engine, which nothing here has.

Ruled out as explanations (both measured, both fine):

- **Connection churn** — the engine pools its postgres connections; 50 queries
  ran against the same backend with an unchanged `backend_start`.
- **HTTP connection churn** — httpx keep-alive works; 31 client requests shared
  one pooled connection (`Request Count: 31`).
- **Concurrency serialization** — throughput improves under load
  (2.5 ms/query sequential → ~1.6 ms/query at 80+ concurrent).

With those ruled out, what remains is structural to the out-of-process design:
a subprocess hop and a GraphQL parse/plan/serialize on every call, plus the
client-side work at each end of it.

### What this means

- **The client-side wins in this document are real but bounded.** Memory (−83%)
  and import time (−92%) are large and worth having. Query latency is not
  where a Python client can win: even eliminating *all* Python deserialization
  would leave ~88% of query time untouched.
- **`query_raw` is the biggest available latency lever** — skipping the
  structured-query path saves 28% (1 row) to 46% (400 rows). Model-scoped
  `Model.prisma().query_raw(...)` still returns typed records, so hot paths can
  use it without giving up the model layer.
- **If query latency is the binding constraint, the out-of-process design is
  the thing to replace, not the client's deserializer.** A driver-level stack
  (SQLAlchemy/psycopg) removes most of the ~90% rather than optimizing the ~10% —
  which is the trade
  [`docs/migrating-to-sqlalchemy.md`](../../docs/migrating-to-sqlalchemy.md)
  lays out. Prisma's value is the schema/typing/migration workflow; this is
  what it costs per query.

## Prisma vs SQLAlchemy Core (`sa_vs_engine.py`)

The same four queries against the same rows, Prisma client vs SQLAlchemy Core
over tables built by `prisma.sa` from the same schema metadata. Interleaved and
direction-alternated, like everything else here.

**Both sides must produce the same thing, or the comparison is not one.** The
SQLAlchemy `find_many_include` runs the three-query shape the engine itself
uses (parents, authors, comments) *and then groups the comments by post and
attaches author and comments to each parent*, inside the timed section — that
is what `include=` means on the Prisma side, and timing only the three SELECTs
would report a smaller amount of work as a faster one. Equivalence is asserted
before timing, per row: same post ids in the same order, same author id on each
post, same comment id set on each post.

What the SQLAlchemy side still does not do is build record objects. That is not
folded into the table; it is measured separately and printed under the results
with its size as a share of the SQLAlchemy total.

> **`results-sa-vs-engine-2026-08-03.json` predates the grouping fix.** It was
> recorded when the SQLAlchemy `find_many_include` returned three unassociated
> row lists, so its `find_many_include` figure — and therefore the total and the
> "% saved" derived from it — describes less work than the query now does. The
> other three queries are unaffected. Re-run the script to get a comparable
> number; the file has deliberately been left as recorded.

## Notes

- Python RSS only; the Rust query engines are separate processes (~22 MB each,
  reported as `engine_rss_mb`) and are unaffected by generator options.
- Bytecode caches are warmed before measuring, so `import_*_ms` is the
  steady-state import cost, not first-deploy compile time.
- Query latencies are medians over `--rounds` iterations per fresh process,
  medians again over `--repeats` processes. They act as the guardrail, not the
  target: generator options shouldn't move engine-side latency, and the loop
  verifies that instead of assuming it.
- The seed dataset is small (400 posts / 800 comments); latencies here measure
  client+engine overhead per query shape, not database performance at scale.
