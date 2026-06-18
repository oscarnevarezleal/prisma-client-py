# Performance benchmarks

Tooling to compare the **upstream** [`prisma-client-py`](https://github.com/RobertCraigie/prisma-client-py)
generator against this fork's optimizations:

- `minimal_runtime` — emit full types into `.pyi` stubs + a slim `.py` runtime
- `scalar_fields_only` — skip relationship fields in generated models
- `separate_model_files` — one lazily-loaded file per model

For an identical synthetic schema, the harness measures each variant's:

| Metric | How |
| --- | --- |
| **Generated code size** | total + per-artifact size of the runtime `.py` files |
| **Cold import time** | wall time to `import prisma.types, prisma.models` in a fresh process |
| **Peak memory** | peak process RSS after that import, interpreter baseline subtracted |

Every measurement runs in a fresh subprocess and is repeated (`--repeats`), reporting the median.

## Usage

```bash
# fast: one interpreter, baseline = optimization flags OFF (≈ upstream output)
python benchmarks/run.py --models 50 --mode flags

# definitive: also install the real upstream release in a throwaway venv
python benchmarks/run.py --models 50 --mode both --repeats 5

# include the separate-model-files variant, save raw JSON
python benchmarks/run.py --models 100 --mode both --separate-model-files --json out.json
```

Options: `--models N`, `--mode {flags,upstream,both}`, `--repeats N`,
`--upstream-version 0.15.0`, `--separate-model-files`, `--workdir DIR`, `--json PATH`.

### Two baselining modes

- **`flags`** — uses only this fork. Baseline generates with
  `minimal_runtime`/`scalar_fields_only`/`separate_model_files` **off**, which reproduces
  upstream-equivalent output; optimized turns them **on**. Same generator and query
  engine, so it isolates exactly the fork's changes. Fast.
- **`upstream`** — `pip install prisma==<version>` into an isolated venv and generates
  with the genuine upstream generator. Truest cross-check. (We verify the upstream
  generator actually ran by asserting it produces **no** `.pyi` stubs — a fork-only artifact.)
- **`both`** — runs upstream + fork-baseline + fork-optimized together.

In `both` mode the `fork-baseline` and `upstream-<ver>` rows should match within noise;
that agreement is what makes the fast `flags` mode trustworthy.

## Representative results

Synthetic schema, each model = 6 scalar fields + a chain relation, `recursive_type_depth = 5`,
Python 3.11. Optimized = `minimal_runtime` + `scalar_fields_only`.

**20 models (`--mode both`)**

| variant | import (ms) | peak RSS (MB) | runtime .py (KB) | types.py (KB) |
| --- | ---: | ---: | ---: | ---: |
| upstream-0.15.0 | 738 | 70.9 | 3050 | 1708 |
| fork-baseline | 772 | 75.9 | 3063 | 1707 |
| **fork-optimized** | **312** | **31.1** | **1006** | **31** |

**50 models (`--mode flags`)**

| variant | import (ms) | peak RSS (MB) | runtime .py (KB) | types.py (KB) |
| --- | ---: | ---: | ---: | ---: |
| fork-baseline | 3185 | 286.5 | 12073 | 9435 |
| **fork-optimized** | **373** | **33.6** | **1761** | **76** |

The win **grows with schema size**: the bulk of upstream's runtime cost is the recursive
query types in `types.py`, which expand super-linearly with model count. `minimal_runtime`
moves those into `.pyi` stubs (type-checkers still see full types) and keeps the runtime slim.

## Notes / caveats

- The harness sets `PRISMA_PY_DEBUG_GENERATOR=1` so generation isn't blocked by a query-engine
  version mismatch. This is safe here because the benchmark **never connects to a database or
  runs queries** — it only measures generated code and import cost.
- Peak RSS is `ru_maxrss` with a bare-interpreter baseline subtracted; treat it as a relative
  comparison, not an absolute footprint.
- `.pyi` stub files are excluded from the runtime-size metric (they aren't imported at runtime),
  but `measure_sizes` records them in the raw JSON.

## Files

- `run.py` — orchestrator (generate each variant, measure, print/emit report)
- `gen_schema.py` — synthetic schema generator (`--models N`)
- `_child.py` — single cold-import measurement, run as a subprocess
