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

See [`benchmarks/`](https://github.com/oscarnevarezleal/prisma-client-py/tree/develop/benchmarks) for the numbers behind the claims below.

## What's different at a glance

| Generator option (schema `generator` block) | Env var | Default here | Upstream |
| --- | --- | --- | --- |
| `minimalRuntime` | `PRISMA_PY_CONFIG_MINIMAL_RUNTIME` | **`true`** | n/a |
| `scalarFieldsOnly` | `PRISMA_PY_CONFIG_SCALAR_FIELDS_ONLY` | `false` | n/a |
| `separateModelFiles` | `PRISMA_PY_CONFIG_SEPARATE_MODEL_FILES` | `false` | n/a |
| `recursiveValidationModels` | `PRISMA_PY_CONFIG_RECURSIVE_VALIDATION_MODELS` | `false` | n/a |

> The one default that changes behavior on upgrade is **`minimalRuntime`, which is on
> by default.** Everything else is opt-in. To reproduce upstream output exactly, set
> `minimalRuntime = false` (see [Rollback](#rollback)).

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

2. **Regenerate the client** — no schema change is required to get `minimalRuntime`:

   ```bash
   python -m prisma generate
   ```

3. **Run your type checker and test suite.** With Pyright/Pylance there should be no
   visible difference (see [Type checking](#type-checking)). If anything looks off,
   jump to [Rollback](#rollback).

That's it for the default win. To go further, enable the opt-in options below.

## The optimizations

### `minimalRuntime` (default: on)

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
  minimalRuntime   = true   # on by default; shown for clarity
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
