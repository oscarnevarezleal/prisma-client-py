# Recursive validation models (design note)

This is an internal design note for a planned generator option that solves the
large-schema memory/startup problem **without giving up runtime validation**. It
records the analysis and the spikes that de-risked it. It is not yet implemented.

## The problem

For large schemas the generated `types.py` explodes super-linearly, because the
recursive query types are *duplicated* `recursive_type_depth` levels deep (a
workaround for mypy, which can't express recursive types). This causes the
long-standing upstream pain:

- huge `types.py` that type checkers refuse to analyze
  ([#1060](https://github.com/RobertCraigie/prisma-client-py/issues/1060)),
- multi-second imports and ~1 GB RAM
  ([#1040](https://github.com/RobertCraigie/prisma-client-py/issues/1040)),
- and, at the extreme, a hard **`RecursionError` on import** so the client can't
  load at all.

The project historically refused to weaken **static** type safety to fix this. But
"static type safety" and "runtime validation" are different concerns that got
coupled into one giant runtime module.

## Two different concerns

| Concern | Lives in | Runtime-validated? |
| --- | --- | --- |
| Record/output (rows → Pydantic models) | `models.py` | **yes** |
| Query arguments (`where`, `data`, `include`) | `types.py` | no — `TypedDict`s, static only |

The bloat is in the query-argument types, which are **static-only**. Moving them to
`.pyi` stubs (`minimalRuntime`, already shipped) costs **zero** runtime validation.
What remains is to stop paying *runtime* memory for the *record* models too — while
keeping their validation.

## The approach: true-recursive, lazily-built models

Pydantic **v2 supports recursive/cyclic models natively** — it does not need the
depth-duplication mypy forces. So:

1. Generate **true-recursive** Pydantic v2 record models (no duplication). Keep the
   depth-expanded forms only in `.pyi` for checkers that need them.
2. Mark them `model_config = ConfigDict(defer_build=True)` and **remove the eager
   `model_rebuild()` loop** at import.
3. Build each model lazily on first use (Pydantic does this automatically), so cost
   scales with *what you touch*, not schema size.

### Why the current approach explodes

The memory blow-up was never inherent to recursive validation — it is the **eager,
per-model `model_rebuild()` at import**, which rebuilds every model's transitive
graph, doing roughly O(N²) redundant work. Measured on a worst-case 200-model chain:

| strategy | 200 models |
| --- | --- |
| eager rebuild, default limit | ❌ `RecursionError` |
| eager rebuild, raised limit | 10.3 s import / 324 MB |
| **lazy build, raised limit** | **0.17 s import / 36 MB, full validation** |

Lazy building changes only *when* a model's validator is compiled (first use instead
of import) — **not** what it checks. Once built, the validator is identical to the
eager one, so nested data still validates fully (verified to 120 levels deep; invalid
nested data is rejected with a precise location such as `next.next.next.f_int`).

## The recursion-limit policy

Building a chain of related models recurses: compiling `Model_i` compiles `Model_{i+1}`
(because of its relation field), and so on. Each level costs a **fixed cluster of
stack frames**, so a chain `D` deep needs `~frames_per_level × D` of Python recursion
limit. Python's default is 1000, which is why deep schemas crash.

`frames_per_level` is constant and measurable, so the generator — which knows the
schema's **maximum relation-chain depth `D`** at generation time — can emit a precise,
**bounded** limit instead of a blanket value:

```
limit = ceil(frames_per_level × D × SAFETY) + HEADROOM
```

Measured on the worst-case chain (`frames_per_level ≈ 14`), `min_build_limit ≈ 14·D + 21`
held steady from D=20 to D=200.

### What one level of recursion looks like

Captured by forcing the build to overflow and grabbing the stack. To compile
`Model_i`, Pydantic processes its `next: Optional[Model_{i+1}]` field and recurses
into compiling `Model_{i+1}`:

```
_generate_schema.py  _model_schema              # build this model's schema
_fields.py           rebuild_model_fields       # walk its fields
_fields.py           _recreate_field_info
_typing_extra.py     eval_type ─┐
typing.py            _eval_type │               # resolve the forward-ref string
typing.py            Optional   │               # "Optional[Model_{i+1}]" into a type
typing.py            Union ─────┘
_generate_schema.py  _generate_md_field_schema  # build a schema for that field
_generate_schema.py  _apply_annotations
_generate_schema.py  match_type
_generate_schema.py  _union_schema              # Optional[X] is a Union, unwrap it
_generate_schema.py  _model_schema              # ...lands back here for Model_{i+1}
```

In four steps per level: **open the model** → **decode the relation's forward-ref
type** → **build a schema for that field** → **recurse into the next model**. The same
cluster repeats once per relation level, which is why the cost is predictable.

### Making it segfault-safe

A recursion limit that's too high risks a C-stack overflow (an uncatchable segfault).
The safe pattern is to build inside a **worker thread with an enlarged stack**:

- with the derived limit, the 200-model chain builds and validates;
- with an *under-provisioned* limit it fails with a **catchable `RecursionError`**
  (which we can surface as a clear "increase the limit" message), not a crash.

The derived limit covers *schema build* depth, which is structural and known at
generation time. Validating pathologically deep *data* payloads is a separate,
runtime/user-controlled concern — the same as in any Pydantic application — and the
enlarged-stack thread protects it too.

## Status

De-risked by two spikes (`benchmarks/spike_recursive_models.py` and
`benchmarks/spike_nested_and_recursion_policy.py`); see
[`benchmarks/README.md`](https://github.com/oscarnevarezleal/prisma-client-py/tree/develop/benchmarks)
for how to reproduce. Not yet wired into the generator. Intended shape: a flag-gated
option (e.g. `recursiveValidationModels = true`) that emits `defer_build` models,
drops the eager rebuild, and applies the derived limit + threaded build.
