"""Clean interleaved comparison of model backends and of end-state configs.

The ladder loop measures each candidate once against a moving baseline, which
is the right shape for exploration but noise-sensitive for close calls. This
script settles specific comparisons: same machine pass, interleaved runs
(A,B,C,A,B,C,... rotated each pass so drift hits all variants equally), higher
repeats.

There are two variant sets, and they answer different questions:

`--set backends` — **which record backend is faster.** The three variants hold
every non-backend generator option identical and differ only in
`modelBackend`. That includes `separateModelFiles`, which is held *off* for
all three: msgspec resolves cyclic relation references against a single
module and so cannot use it, and leaving it on for the other two would fold a
lazy-per-model-file win into a number labelled "backend".

`--set configs` — **what an end-state configuration costs.** Each variant is
the best known configuration for its backend, so they differ in more than the
backend (`config-pydantic` keeps `separateModelFiles` and no `lazyActions`;
`config-final` adds runtime env flags). Rows here are comparable to
`baseline` — an upstream default install — but *not* to each other as a
backend comparison.

    python head2head.py --workdir /tmp/pglab --set backends --repeats 7 --rounds 30
"""

from __future__ import annotations

import sys
import json
import argparse
import statistics
import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent

_spec = importlib.util.spec_from_file_location('optimize_loop', HERE / 'optimize_loop.py')
assert _spec and _spec.loader
_loop = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_loop)

# Held identical across every `backend-*` variant, so `modelBackend` is the only
# thing that differs between them. Do not add an option here that one backend
# cannot take — hold it off for all three instead, or the comparison stops
# being a backend comparison.
BACKEND_COMMON: dict[str, str] = {
    'recursive_type_depth': '-1',
    'minimalRuntime': 'true',
    'separateModelFiles': 'false',
    'lazyActions': 'true',
}

# Each variant: generator `options`, whether the two interfaces are grafted into
# one `unified` package, and any runtime `env` flags.
BACKEND_VARIANTS: dict[str, dict[str, object]] = {
    'backend-pydantic': {'options': dict(BACKEND_COMMON), 'unified': True, 'env': {}},
    'backend-slim': {
        'options': {**BACKEND_COMMON, 'modelBackend': '"slim"'},
        'unified': True,
        'env': {},
    },
    'backend-msgspec': {
        'options': {**BACKEND_COMMON, 'modelBackend': '"msgspec"'},
        'unified': True,
        'env': {},
    },
}

# End-state configurations. These are NOT a controlled comparison of anything:
# each is the best known setup for its backend, and they differ in several
# options at once. Read them against `baseline`, not against each other.
CONFIG_VARIANTS: dict[str, dict[str, object]] = {
    # what an upstream user gets today: default options, two separate packages
    'baseline': {'options': {}, 'unified': False, 'env': {}},
    # generator options only (the phase-1 winner)
    'config-pydantic': {
        'options': {'recursive_type_depth': '-1', 'minimalRuntime': 'true', 'separateModelFiles': 'true'},
        'unified': True,
        'env': {},
    },
    'config-slim': {
        'options': {
            'recursive_type_depth': '-1',
            'minimalRuntime': 'true',
            'separateModelFiles': 'true',
            'lazyActions': 'true',
            'modelBackend': '"slim"',
        },
        'unified': True,
        'env': {},
    },
    'config-msgspec': {
        'options': {
            'recursive_type_depth': '-1',
            'minimalRuntime': 'true',
            'separateModelFiles': 'false',
            'lazyActions': 'true',
            'modelBackend': '"msgspec"',
        },
        'unified': True,
        'env': {},
    },
    # everything this fork offers, turned on together
    'config-final': {
        'options': {
            'recursive_type_depth': '-1',
            'minimalRuntime': 'true',
            'separateModelFiles': 'false',
            'lazyActions': 'true',
            'modelBackend': '"msgspec"',
        },
        'unified': True,
        'env': {'PRISMA_PY_SHARED_ENGINE': '1', 'PRISMA_PY_RAW_DECODE': '1'},
    },
}

VARIANTS: dict[str, dict[str, object]] = {**CONFIG_VARIANTS, **BACKEND_VARIANTS}

SETS: dict[str, dict[str, dict[str, object]]] = {
    'backends': BACKEND_VARIANTS,
    'configs': CONFIG_VARIANTS,
    'all': VARIANTS,
}

HEADERS = ('| variant | RSS (MB) | import (ms) | connect (ms) | queries (ms) | composite | vs baseline |',
           '| --- | ---: | ---: | ---: | ---: | ---: | ---: |')


def print_table(title: str, caveat: str, results: dict[str, dict[str, float]],
                base_c: dict[str, float] | None) -> None:
    print()
    print(f'### {title}')
    print(caveat)
    print()
    for line in HEADERS:
        print(line)
    for name, c in results.items():
        delta = ''
        if base_c and name != 'baseline':
            delta = f'{(c["composite"] / base_c["composite"] - 1) * 100:+.1f}%'
        print(
            f'| {name} | {c["rss_final_mb"]} | {c["import_total_ms"]} '
            f'| {c["connect_total_ms"]} | {c["query_total_ms"]} | {c["composite"]} | {delta} |'
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--workdir', required=True)
    parser.add_argument('--repeats', type=_loop.positive_int, default=7)
    parser.add_argument('--rounds', type=_loop.positive_int, default=30)
    parser.add_argument('--json', default=None)
    parser.add_argument('--set', dest='variant_set', choices=sorted(SETS), default='all',
                        help='backends: modelBackend isolated; configs: end-state configs vs baseline')
    parser.add_argument('--only', nargs='+', default=None, help='subset of variant names to run')
    args = parser.parse_args()

    base = Path(args.workdir)

    chosen = SETS[args.variant_set]
    if args.only:
        unknown = [name for name in args.only if name not in chosen]
        if unknown:
            parser.error(
                f'--only: unknown variant(s) {", ".join(unknown)} in set {args.variant_set!r}; '
                f'available: {", ".join(chosen)}'
            )
    selected = {k: v for k, v in chosen.items() if args.only is None or k in args.only}

    # generate every variant into its own subdirectory up front so the
    # measurement phase can interleave without regeneration between runs
    mods: dict[str, tuple[Path, str, str, dict]] = {}
    for name, spec in selected.items():
        workdir = base / f'h2h_{name}'
        workdir.mkdir(parents=True, exist_ok=True)
        a, s = _loop.generate_variant(workdir, dict(spec['options']), bool(spec.get('unified', True)))  # type: ignore[arg-type]
        mods[name] = (workdir, a, s, dict(spec.get('env', {})))  # type: ignore[arg-type]
        print(f'generated {name}', flush=True)

    raw: dict[str, list[dict[str, float]]] = {name: [] for name in mods}
    for i in range(args.repeats):
        # Rotate the running order by pass index. Reversing on alternate passes
        # only ever gives a variant two of the available positions — with three
        # or more variants the middle ones stay in the middle every pass, and
        # whatever the machine is doing during that slot biases the same
        # variant every time. A rotation walks each variant through every slot.
        items = list(mods.items())
        offset = i % len(items)
        items = items[offset:] + items[:offset]
        for name, (workdir, a, s, env) in items:
            m = _loop.measure(workdir, a, s, 1, args.rounds, env)
            raw[name].append(m)
        print(f'pass {i + 1}/{args.repeats} done', flush=True)

    results = {}
    for name, runs in raw.items():
        med = {k: round(statistics.median(r[k] for r in runs), 2) for k in runs[0]}
        results[name] = _loop.cost(med)

    base_c = results.get('baseline')
    configs = {k: v for k, v in results.items() if k in CONFIG_VARIANTS}
    backends = {k: v for k, v in results.items() if k in BACKEND_VARIANTS}

    if configs:
        print_table(
            'End-state configurations',
            'Each row is the best known setup for its backend and differs from the others in more\n'
            'than the backend. Compare against `baseline`; do not read this table as a backend ranking.',
            configs, base_c,
        )
    if backends:
        print_table(
            'Model backends (only `modelBackend` differs)',
            'Every other generator option is held identical, including `separateModelFiles`, which is\n'
            'off for all three because msgspec cannot use it.',
            backends, base_c,
        )

    if base_c:
        print()
        for name, c in results.items():
            if name == 'baseline':
                continue
            print(
                f'{name}: RSS {base_c["rss_final_mb"]:.1f} -> {c["rss_final_mb"]:.1f} MB '
                f'({(c["rss_final_mb"] / base_c["rss_final_mb"] - 1) * 100:+.0f}%), '
                f'import {base_c["import_total_ms"]:.0f} -> {c["import_total_ms"]:.0f} ms '
                f'({(c["import_total_ms"] / base_c["import_total_ms"] - 1) * 100:+.0f}%)'
            )

    if args.json:
        Path(args.json).write_text(json.dumps(
            {
                'set': args.variant_set,
                'options': {name: selected[name]['options'] for name in results},
                'env': {name: selected[name].get('env', {}) for name in results},
                'results': results,
                'raw': raw,
            },
            indent=2,
        ))


if __name__ == '__main__':
    sys.exit(main())
