# Design note: one client for sync *and* async

**Status:** analysis / proposal. Nothing here is implemented.

## The problem

`interface` is a whole-generator option, not a per-call-site one:

```prisma
generator client {
  provider  = "prisma-client-py"
  interface = "asyncio"   # or "sync" — pick exactly one
}
```

An application that needs both a blocking entrypoint (a CLI, a Celery worker, a
sync-only framework, a migration script) and an asyncio one (a web server) therefore
has to generate the client **twice**, into two packages:

```prisma
generator async_client { provider = "prisma-client-py"  interface = "asyncio"  output = "../app/db_async" }
generator sync_client  { provider = "prisma-client-py"  interface = "sync"     output = "../app/db_sync"  }
```

Every process that imports both then pays for two full copies of the Pydantic model
layer and the query-type layer — even though those layers contain no `async` anything.

## What actually differs between the two builds

Measured on a 20-model synthetic schema (`benchmarks/dual_interface.py`), comparing
the two generated packages file by file: **76.8% of the emitted runtime `.py` bytes are
byte-identical.**

| Generated artifact | Differs? | 20-model size |
| --- | :-: | ---: |
| `types.py` | **no** | 1,708 KB |
| `models.py` | **no** | 146 KB |
| `bases.py` | **no** | 10 KB |
| `enums.py`, `partials.py`, `metadata.py` | **no** | small |
| the entire vendored runtime (`_base_client.py`, `_builder.py`, `_transactions.py`, `engine/_query.py`, …) | **no** | ~450 KB |
| `actions.py` | yes | 633 KB |
| `client.py` | yes | 75 KB |
| `http.py`, `engine/abstract.py`, `engine/query.py`, `engine/http.py` | yes | < 2 KB combined |

The last four are not real code — they are one-line aliasing shims:

```jinja
{# engine/query.py.jinja #}
{% if is_async %}
from ._query import AsyncQueryEngine
QueryEngine = AsyncQueryEngine
{% else %}
from ._query import SyncQueryEngine
QueryEngine = SyncQueryEngine
{% endif %}
```

So the genuinely interface-specific surface is exactly two files: `actions.py` and
`client.py`. Both are produced from the same templates with `maybe_await` /
`maybe_async_def` toggled by `_utils.py.jinja`.

**The runtime library already ships both halves.** `_base_client.py` exports
`SyncBasePrisma` *and* `AsyncBasePrisma`; `_transactions.py` exports both transaction
managers; `engine/_query.py` and `engine/_http.py` export both engines; `_sync_http.py`
and `_async_http.py` are both installed. Nothing is stripped at generation time —
`interface` only decides which of the already-present halves the generated shims point at.

There is even a vestigial hook for this in the generator:

```py
class PythonNames(BaseModel):
    def client_class(self, _for_async: bool) -> str:
        return 'Prisma'
```

`_for_async` is threaded through every template that names the client class
(`client.py.jinja`, `actions.py.jinja`, `bases.py.jinja`) and then ignored. It is the
seam a dual-interface build would use to emit `Prisma` and `SyncPrisma`.

## Measured impact

`benchmarks/dual_interface.py` generates both clients, then builds a *unified*
prototype by grafting the sync build's `actions.py`/`client.py` onto the async build's
package as `actions_sync.py`/`client_sync.py` and rewiring their relative imports, so
both interfaces share one copy of `models.py`, `types.py` and `bases.py`.

Retained RSS (not peak — what stays resident once both clients are up), Python 3.11,
`recursive_type_depth = 5`, medians:

| models | `minimalRuntime` | first client | **+ 2nd interface, today** | **+ 2nd interface, unified** | total |
| ---: | :-: | ---: | ---: | ---: | --- |
| 20 | off | 80.2 MB | **+42.1 MB** | **+0.3 MB** | 122.3 → 80.4 MB (**−34%**) |
| 20 | on | 42.9 MB | **+8.6 MB** | **+0.7 MB** | 51.5 → 43.6 MB (**−15%**) |
| 50 | off | 267.8 MB | **+217.8 MB** | **+2.3 MB** | 485.6 → 270.1 MB (**−44%**) |
| 50 | on | 60.2 MB | **+25.7 MB** | **+1.9 MB** | 85.9 → 62.1 MB (**−28%**) |

On disk, over the same matrix, the dual build's runtime `.py` goes 6.0 → 3.7 MB
(20 models), 23.6 → 13.5 MB (50 models), and the byte-overlap between the two builds
is 77% at 20 models and 85% at 50.

Three things to read out of this:

1. **The marginal cost of the second interface collapses by ~99%** — from 42 MB to
   0.3 MB at 20 models, from 218 MB to 2.3 MB at 50. What remains is the genuine
   `actions.py` + `client.py` duplication, which is almost entirely docstrings.

2. **The saving scales with schema size, because the duplicated part is the part that
   scales.** `types.py` grows super-linearly with model count (see
   [`benchmarks/README.md`](https://github.com/oscarnevarezleal/prisma-client-py/tree/develop/benchmarks));
   `actions.py` and `client.py` grow linearly. Dual-interface users are the ones who hit
   the memory wall documented in upstream
   [#620](https://github.com/RobertCraigie/prisma-client-py/issues/620) at half the
   schema size everyone else does.

3. **It composes with `minimalRuntime` rather than competing with it.** The two attack
   different halves: `minimalRuntime` shrinks the *first* copy, unification removes the
   *second*. Applied together the pair takes 122.3 MB → 43.6 MB at 20 models, and
   485.6 MB → 62.1 MB at 50.

The prototype is functionally sound — both clients construct, `find_many` is a
coroutine function on one and an ordinary function on the other, they inherit
`AsyncBasePrisma` / `SyncBasePrisma` respectively, and `async_client.user._model is
sync_client.user._model` (one shared Pydantic class, which is the whole point).

## What a real implementation has to solve

The prototype is a measurement device, not a design. Four things it papers over:

### 1. The registry is single-slot

`_registry._registered_client` is one module-level global, and `register()` raises
`ClientAlreadyRegisteredError` on a second call. Today two packages means two
independent `_registry` modules, so both flavours can be registered. Unify the package
and they collide.

`get_client()` also does `isinstance(registered, Prisma)` against a single imported
`Prisma` class. Both need to become flavour-aware — either two slots
(`register(client)` dispatching on `isinstance(client, AsyncBasePrisma)`), or a keyed
registry. Two slots is the smaller change and keeps `register()`'s "call this once"
guarantee per flavour.

### 2. `Model.prisma()` becomes ambiguous

`bases.py` hardcodes the model-scoped accessor to one flavour:

```py
@classmethod
def prisma(cls, client: Optional['Prisma'] = None) -> 'actions.UserActions[_PrismaModelT]':
    from .client import get_client
    return actions.UserActions[_PrismaModelT](client or get_client(), cls)
```

This is *why* `bases.py` is currently byte-identical across builds — `client_class()`
returns `'Prisma'` either way, so the async and sync renderings coincide by accident.
Under a unified package it has to resolve to something. The options, roughly in order
of preference:

- **Add a second accessor** (`User.prisma()` async, `User.prisma_sync()` sync). Explicit,
  statically typeable, no runtime dispatch. Costs a second method per model in
  `bases.py` — cheap, since `bases.py` is 10 KB at 20 models.
- **Dispatch on the registered client's type.** Preserves the existing API exactly, but
  the return type can't be expressed statically, which forfeits the type safety that is
  most of the reason to use this client.
- **Only support the instance-scoped API** (`db.user.find_many(...)`) for the second
  flavour. Smallest change, but `Model.prisma()` is a documented and widely used entry
  point.

### 3. `actions.py` is still duplicated, and it is large

633 KB at 20 models, growing linearly. It costs ~0.3–2.3 MB of RSS (the measurements
above) but it doubles the file a type checker has to analyse — the exact complaint in
upstream [#1060](https://github.com/RobertCraigie/prisma-client-py/issues/1060). Most
of that bulk is docstrings; the `minimalRuntime` stub-splitting machinery
(`STUB_FILE_TEMPLATES` in `generator/generator.py`) already knows how to move type-level
weight into `.pyi`, and `actions_sync.py` is a natural candidate for the same treatment.

### 4. Naming, exports, and the mypy plugin

`client.py` exports `Prisma`, `Client`, `Batch`, `TransactionManager` at module scope.
A second flavour in the same package needs a non-colliding set (`SyncPrisma`,
`SyncBatch`, …) and `__init__.py` needs to re-export both without making
`from prisma import Prisma` ambiguous. `src/prisma/mypy.py` and the `typesafety/` tests
both pattern-match on the generated client and will need updating.

## Proposed shape

Add a third `interface` value rather than a new option, since it is the same axis:

```prisma
generator client {
  provider  = "prisma-client-py"
  interface = "both"
}
```

Generation changes:

| File | `interface = "both"` |
| --- | --- |
| `models.py`, `types.py`, `enums.py`, `partials.py`, `metadata.py` | unchanged, single copy |
| `bases.py` | emit `prisma()` **and** `prisma_sync()` |
| `actions.py` | async, as today |
| `actions_sync.py` | new — same template, `is_async = false` |
| `client.py` | async `Prisma`, plus `SyncPrisma` importing from `actions_sync` |
| `http.py`, `engine/*.py` shims | export both names instead of aliasing one |
| `_registry.py` | two slots, dispatched on base class |

Mechanically this is mostly a loop over `[True, False]` around the two templates that
consume `is_async`, plus finally using the `client_class(is_async)` argument that is
already threaded everywhere. The template work is small; items 1 and 2 above are where
the actual design decisions are.

Keep it off by default, consistent with every other option in this fork — `"sync"` and
`"asyncio"` must keep producing byte-identical output to what they produce today.

## Alternatives considered

**Sync wrapper over the async client** (run the async client on a background event
loop, expose blocking methods). One generated flavour, no duplication at all. Rejected:
it forces an event loop into every sync process, deadlocks if the caller already has a
running loop, and makes tracebacks and cancellation semantics substantially worse. The
per-flavour `actions.py` is a much cheaper price than that.

**Extract models/types into a third shared package** consumed by two client packages.
No generator changes to `is_async` handling at all, and it works today with a
post-generation script. Rejected as the primary answer: it needs a third generator
block plus import rewriting, the two clients still can't share a registry, and users
have to understand a three-package layout. It is, however, a reasonable *stopgap* for
anyone blocked on this before `interface = "both"` exists.

**Do nothing, rely on `minimalRuntime`.** It does cut the dual-interface cost
substantially (51.5 MB vs 122.3 MB at 20 models). But the duplication is still there and
still scales — at 50 models the second copy alone is 218 MB unoptimised, 25.7 MB even
with `minimalRuntime` on — and it is duplication of code that is *provably identical*,
which is the unsatisfying part.

## Reproducing

```bash
python benchmarks/dual_interface.py --models 20 50 --repeats 3 --minimal-runtime
```

Reports the byte-overlap between the two builds, on-disk size, and the marginal RSS of
the second interface under both layouts. Peak vs retained RSS matters here: warm the
bytecode caches first, or compiling the multi-megabyte `types.py` from source dominates
the measurement (the harness does this for you).
