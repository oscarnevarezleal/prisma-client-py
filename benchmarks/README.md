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
`--recursive-type-depth N`, `--upstream-version 0.15.0`, `--separate-model-files`,
`--workdir DIR`, `--json PATH`.

### Recursive type depth

`--recursive-type-depth` (default `5`) is applied to every variant. The amount of
generated type bloat is dominated by this setting, so it's the key axis to vary:

- `5` (default) — the bloat is large enough that at ~100 models the **un-optimized**
  client becomes *unimportable* (Pydantic `RecursionError`).
- `-1` — "true recursive types", the maintainer's recommended workaround for large
  schemas (Pyright only; see upstream discussion #867). This shrinks the baseline a lot,
  so it's the fairest comparison for *"does the fork still help on top of the official
  advice?"* — and it does, just more modestly.

Example: `python benchmarks/run.py --models 50 --mode both --recursive-type-depth -1`

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

Synthetic schema (each model = 6 scalar fields + a chain relation), Python 3.11,
optimized = `minimal_runtime` + `scalar_fields_only`. Numbers are medians.

### Consolidated matrix

`baseline` is upstream-equivalent (verified against a real upstream install in `both` mode;
the two agree within noise). "unimportable" = the un-optimized client raises Pydantic
`RecursionError` on import at that scale.

**peak RSS (MB) — baseline → optimized**

| models | depth `5` | depth `-1` |
| ---: | --- | --- |
| 20  | 75.9 → **31.1**  (−59%) | 50.8 → **35.6**  (−30%) |
| 50  | 286.5 → **33.6**  (−88%) | 109.2 → **33.4**  (−69%) |
| 100 | *unimportable* → **35.4** | *unimportable* → **34.9** |

**`types.py` size (KB) — baseline → optimized**

| models | depth `5` | depth `-1` |
| ---: | --- | --- |
| 20  | 1,707 → **31** | 479 → **31** |
| 50  | 9,435 → **76** | 2,186 → **76** |
| 100 | 36,170 → **150** | 7,711 → **150** |

**import time (ms) — baseline → optimized**

| models | depth `5` | depth `-1` |
| ---: | --- | --- |
| 20  | 772 → **312** | 636 → **387** |
| 50  | 3,185 → **373** | 1,705 → **360** |
| 100 | *unimportable* → **392** | *unimportable* → **414** |

### Takeaways

- **The optimized client stays roughly flat** (~31–36 MB RSS, ~150 KB `types.py`, ~0.4 s import)
  as the schema grows, while the baseline explodes super-linearly until it fails to import.
- **The win grows with schema size.** Most of the baseline's cost is the recursive query
  types in `types.py`; `minimal_runtime` moves those into `.pyi` stubs (type-checkers still
  see full types) and keeps the runtime slim.
- **`recursive_type_depth = -1` (the maintainer's workaround) helps but isn't enough at scale.**
  It shrinks the baseline ~5× and is importable for small/medium schemas, but at 100 chained
  models it *still* hits `RecursionError` — the optimized variant is what keeps the client
  loadable. (See the caveat below about chain topology.)

## Notes / caveats

- The harness sets `PRISMA_PY_DEBUG_GENERATOR=1` so generation isn't blocked by a query-engine
  version mismatch. This is safe here because the benchmark **never connects to a database or
  runs queries** — it only measures generated code and import cost.
- Peak RSS is `ru_maxrss` with a bare-interpreter baseline subtracted; treat it as a relative
  comparison, not an absolute footprint.
- `.pyi` stub files are excluded from the runtime-size metric (they aren't imported at runtime),
  but `measure_sizes` records them in the raw JSON.
- The synthetic schema chains models linearly (`Model0 → Model1 → … → ModelN`), a near-worst-case
  for recursive-type resolution. The import-time `RecursionError` at 100 models is the extreme
  tail; the size and flat-memory results are the robustly generalizable ones. Real schemas with
  shallower relation graphs may import the baseline fine at the same model count.

## Files

- `run.py` — orchestrator (generate each variant, measure, print/emit report)
- `gen_schema.py` — synthetic schema generator (`--models N`)
- `_child.py` — single cold-import measurement, run as a subprocess
