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
```

Each loop iteration emits one JSON line (`ACCEPT`/`REJECT` + scores); the run
ends with a `DONE` summary and regenerates the winning configuration in the
workdir.

## The candidate ladder

Applied cumulatively, in expected-value order; a candidate that fails to
generate or crashes the workload is auto-rejected (that's the filter that
catches behavior-changing options like `scalarFieldsOnly` under an
`include=`-heavy workload):

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
