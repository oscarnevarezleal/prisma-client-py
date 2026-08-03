# Migrating from upstream prisma-client-py

This fork is a drop-in replacement for
[`RobertCraigie/prisma-client-py`](https://github.com/RobertCraigie/prisma-client-py)
that adds opt-in generator options to tame the memory/size/startup cost of large
schemas — the long-standing pain reported upstream in
[#620](https://github.com/RobertCraigie/prisma-client-py/issues/620) (memory),
[#1040](https://github.com/RobertCraigie/prisma-client-py/issues/1040) (slow cold start),
and [#1060](https://github.com/RobertCraigie/prisma-client-py/issues/1060) (type checker
chokes on a huge `types.py`).

**Your application code does not change.** The query API, model classes, and import
paths (`from prisma import Prisma`, `from prisma.models import User`) are identical.
What changes is how the client is *generated*.

See [Performance Findings](performance-findings.md) for the consolidated measurements —
what worked, what didn't, and where a Prisma query actually spends its time —
or [`benchmarks/`](https://github.com/oscarnevarezleal/prisma-client-py/tree/develop/benchmarks)
for the harnesses behind the claims below.

## What's different at a glance

| Generator option (schema `generator` block) | Env var | Default here | Upstream |
| --- | --- | --- | --- |
| `minimalRuntime` | `PRISMA_PY_CONFIG_MINIMAL_RUNTIME` | `false` | n/a |
| `scalarFieldsOnly` | `PRISMA_PY_CONFIG_SCALAR_FIELDS_ONLY` | `false` | n/a |
| `separateModelFiles` | `PRISMA_PY_CONFIG_SEPARATE_MODEL_FILES` | `false` | n/a |
| `recursiveValidationModels` | `PRISMA_PY_CONFIG_RECURSIVE_VALIDATION_MODELS` | `false` | n/a |
| `lazyActions` | `PRISMA_PY_CONFIG_LAZY_ACTIONS` | `false` | n/a |
| `modelBackend` (experimental) | `PRISMA_PY_CONFIG_MODEL_BACKEND` | `"pydantic"` | n/a |

Two further opt-ins are runtime environment flags rather than generator options:

| Runtime flag | Default | What it does |
| --- | --- | --- |
| `PRISMA_PY_SHARED_ENGINE=1` | off | sync + async clients for the same schema/datasource share one query-engine process (refcounted); saves ~an engine process (~20-25 MB) and a spawn per extra client connected at the same time |
| `PRISMA_PY_FAST_PARSE=1` | off | deserialize trusted engine responses without a validation pass. **Measured slower than pydantic-core validation on Pydantic v2** — kept for completeness, not recommended |
| `PRISMA_PY_RAW_DECODE=1` | off | decode engine response bytes straight into records, skipping the intermediate dict. Requires `modelBackend = "msgspec"`; a no-op otherwise. **7-14% faster on bulk reads** (200+ rows), neutral on small ones — see below |

### `PRISMA_PY_RAW_DECODE` (runtime flag, msgspec backend only)

Normally a query costs `bytes -> dict -> records`. With this flag the client
decodes the engine's raw bytes directly into record structs in one pass.

It is a **bulk-read** optimization. Deserialization is only ~5% of a small
query (the engine round-trip is ~93%), but its share grows with result size,
so the win lands where result sets are large: ~10% at 200 rows, 7-14% at 400.
Small responses stay on the plain path automatically — below
`PRISMA_PY_RAW_DECODE_MIN_BYTES` (default `20000`) the typed decoder's fixed
cost would exceed its saving — so enabling it never makes small queries slower.

Aggregate methods (`count`, `group_by`, `*_many`) are excluded by design; they
return counts rather than records. Errors, transactions, `include` relations
and `Decimal`/`datetime` coercion all behave identically to the dict path.

### `lazyActions` (default: off)

Defers creating the per-model action namespaces (`client.user`, `client.post`, …)
and importing the actions module until first database access. `Prisma()`
construction and `import` become O(models you touch) instead of O(models in the
schema). No API change; the first query on each model pays a one-time lookup.

### `modelBackend` (default: `"pydantic"`, experimental)

Selects how record models are generated. Both alternative backends keep the
pydantic-shaped surface most code relies on (`model_dump()`, `dict()`, `json()`,
keyword construction, `Model.prisma()`), but **convert trusted engine data
rather than validating arbitrary input** — keep the default backend where you
feed untrusted data into models, or validate at the boundary. Not supported on
either: `create_partial()`, subclass field overrides, the mypy plugin's model
checks.

**`"msgspec"` — recommended alternative.** Generates
[msgspec](https://jcristharif.com/msgspec/) `Struct` records decoded in C.
Against the default backend it is ~8 MB lighter and modestly faster; against
`"slim"` it is **statistically tied** (they finish within ~4% and the ordering
has flipped between measurement runs), so choose on constraints rather than on
the score — msgspec is a maintained C library and is the faster of the two on
bulk deserialization. Requires `pip install prisma[msgspec]` (or
`msgspec` directly) and is incompatible with `separateModelFiles` (cyclic
relation references must resolve against a single module — cheap, since structs
compile no per-model schemas). Records additionally offer `to_pydantic()`,
returning a real `pydantic.BaseModel` (lazily built, cached twin class) for
integrations that require one, e.g. FastAPI `response_model`.

```prisma
generator client {
  provider     = "prisma-client-py"
  modelBackend = "msgspec"
}
```

**`"slim"` — zero-dependency alternative.** Pydantic-free `__slots__` records
deserialized by exec-compiled per-model converters. Marginally lower RSS than
msgspec and no third-party dependency, at the cost of hand-rolled
deserializers this fork maintains itself. Requires `separateModelFiles = true`.

See [`benchmarks/pg-lab/`](https://github.com/oscarnevarezleal/prisma-client-py/tree/develop/benchmarks/pg-lab)
for the head-to-head measurements behind these recommendations.

> **All options are opt-in (off by default), so installing the fork and regenerating
> reproduces upstream output exactly.** Enable the options below as needed.

## Step-by-step

1. **Install the fork** (replace the branch with the one you track):

   ```bash
   pip uninstall prisma
   # general optimizations:
   pip install "git+https://github.com/oscarnevarezleal/prisma-client-py.git@develop"
   # to also get recursiveValidationModels (still on its feature branch):
   pip install "git+https://github.com/oscarnevarezleal/prisma-client-py.git@claude/eager-hamilton-7a4gge"
   ```

   > `recursiveValidationModels` currently lives on the
   > `claude/eager-hamilton-7a4gge` branch; once it lands on `develop`, install that.
   > It also **requires Pydantic v2**.

2. **Regenerate the client** — output is upstream-equivalent until you enable an option:

   ```bash
   python -m prisma generate
   ```

3. **Run your type checker and test suite.** With Pyright/Pylance there should be no
   visible difference (see [Type checking](#type-checking)). If anything looks off,
   jump to [Rollback](#rollback).

Nothing changes by default. Enable the opt-in options below as your schema needs them.

## The optimizations

### `minimalRuntime` (default: off)

Splits each heavy generated module into a `.pyi` stub with the **full** types (what your
type checker reads) and a slim `.py` with a minimal runtime. The recursive query-argument
types in `types.py` (`WhereInput`, `CreateInput`, `Include`, `Select`, …) — which dominate
the file size and blow up super-linearly with schema size — are aliased to a lightweight
`dict` subclass at runtime instead of being materialized as giant `TypedDict`s.

- **What you keep:** record/model **validation is fully preserved** — `models.py` and its
  Pydantic classes are unchanged, so `User(**bad_data)` still raises `ValidationError`. The
  query-argument types that become runtime dicts were never validated by Pydantic anyway;
  they are static typing only, and your type checker still sees them in full via the `.pyi`.
- **What you trade:** runtime introspection of those query-argument types (rare). If you do
  *not* use a `.pyi`-aware type checker, you lose static checking on query **arguments**
  (not on records).

### `scalarFieldsOnly` (default: off, most aggressive)

Omits relationship fields from the generated model classes, avoiding the circular
validation graphs that inflate memory. **This changes runtime behavior:** model instances
won't carry relation attributes, and relations aren't validated (your database's foreign
keys still are). Enable it only after confirming your code doesn't rely on accessing
related records off a fetched model instance, and test thoroughly.

```prisma
generator client {
  provider         = "prisma-client-py"
  scalarFieldsOnly = true
}
```

### `separateModelFiles` (default: off)

Generates one lazily-loaded file per model (`models/_user.py`, …) instead of a single
`models.py`, so only the models you actually touch are imported. Import syntax is
unchanged: `from prisma.models import User` still works. This directly addresses upstream
[#1060](https://github.com/RobertCraigie/prisma-client-py/issues/1060)'s "split the types
file per model" request and helps type checkers that refuse to analyze one huge file.

```prisma
generator client {
  provider           = "prisma-client-py"
  separateModelFiles = true
}
```

### `recursiveValidationModels` (default: off, Pydantic v2 only)

For **large or deeply-related schemas**, the record models in `models.py` are
duplicated `recursive_type_depth` levels deep and all rebuilt eagerly at import. Past a
point this makes `import prisma.models` slow, memory-heavy, or fail outright with
`RecursionError: maximum recursion depth exceeded` — so the client can't even load.

This option generates **true-recursive Pydantic v2 models** that are compiled **lazily on
first use** (`defer_build=True`) instead of all at import:

- **What you keep:** **full runtime validation**, including nested relations — invalid data
  is still rejected with a precise location (e.g. `loc=('next','next','f_int')`). This is
  not `scalarFieldsOnly`; relation fields are still present and validated.
- **What changes:** the first time you use a given model, its validator is built (one-time,
  ~tens to ~hundred ms for very deep chains, then cached). Memory scales with the models you
  actually touch, not the schema size, and stays roughly flat.
- **Safety:** the build runs in a worker thread with an enlarged stack sized to your schema's
  longest relation chain, so deep schemas build safely; if the chain is deeper than estimated
  you get a catchable `RecursionError` with guidance, never a segfault.
- **Pairs with `minimalRuntime`:** they fix the two halves — `minimalRuntime` keeps `types.py`
  from exploding, `recursiveValidationModels` keeps `models.py` from exploding.

```prisma
generator client {
  provider                  = "prisma-client-py"
  minimalRuntime            = true
  recursiveValidationModels = true
}
```

Reach for this if you have roughly **100+ models** or long relation chains, or if upstream
already fails to import for you. Smaller schemas don't need it (default off).

### Recommended starting point

```prisma
generator client {
  provider         = "prisma-client-py"
  interface        = "asyncio"
  minimalRuntime   = true   # opt-in; the biggest size/import win
  scalarFieldsOnly = false  # turn on only if you don't read relations off instances
}
```

For a **large/deep schema** (≈100+ models, or upstream already fails to import), add
`recursiveValidationModels = true` (Pydantic v2).

## Type checking

The full types live in `.pyi` stubs. **Pyright/Pylance and mypy both read `.pyi` stubs**
that sit next to the `.py` file, so static type checking is preserved. For very large
schemas, Pyright remains the recommended checker (mypy struggles with the deeply recursive
types regardless of this fork — see upstream
[#813](https://github.com/RobertCraigie/prisma-client-py/issues/813)). You can also combine
`minimalRuntime` with `recursive_type_depth = -1` for the smallest stubs.

## Compatibility notes

- **Python 3.8 / 3.9:** supported. (The fork's lazy-loading `__init__` and generated
  modules use `from __future__ import annotations` so runtime-subscripted hints don't break
  on 3.8/3.9.)
- **Query engine:** unchanged from upstream — same pinned Prisma engine, same database
  behavior. Only code generation differs.
- **Pydantic v1 and v2:** both supported, as upstream.

## Rollback

To get upstream-identical generated output, disable the default and regenerate:

```prisma
generator client {
  provider       = "prisma-client-py"
  minimalRuntime = false
}
```

or per-invocation without touching the schema:

```bash
PRISMA_PY_CONFIG_MINIMAL_RUNTIME=false python -m prisma generate
```

To leave the fork entirely, `pip install prisma==<version>` from PyPI and regenerate.

## Verifying the win on your own schema

```bash
# fast: toggles the fork's flags off/on (off ≈ upstream output)
python benchmarks/run.py --models <n> --mode flags

# definitive: also installs the real upstream release in a throwaway venv
python benchmarks/run.py --models <n> --mode both --recursive-type-depth -1
```

See [`benchmarks/README.md`](https://github.com/oscarnevarezleal/prisma-client-py/tree/develop/benchmarks/README.md) for how the harness works and the
representative results matrix.
