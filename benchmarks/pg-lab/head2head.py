"""Clean head-to-head of model backends on the phase-2 end-state configs.

The ladder loop measures each candidate once against a moving baseline, which
is the right shape for exploration but noise-sensitive for close calls. This
script settles a specific comparison: same machine pass, interleaved runs
(A,B,C,A,B,C,... so drift hits all variants equally), higher repeats.

    python head2head.py --workdir /tmp/pglab --repeats 7 --rounds 30
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

# Each variant: generator `options`, whether the two interfaces are grafted into
# one `unified` package, and any runtime `env` flags.
VARIANTS: dict[str, dict[str, object]] = {
    # what an upstream user gets today: default options, two separate packages
    'baseline': {'options': {}, 'unified': False, 'env': {}},
    'pydantic': {
        'options': {'recursive_type_depth': '-1', 'minimalRuntime': 'true', 'separateModelFiles': 'true'},
        'unified': True,
        'env': {},
    },
    'slim': {
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
    'msgspec': {
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
    'final': {
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workdir', required=True)
    parser.add_argument('--repeats', type=int, default=7)
    parser.add_argument('--rounds', type=int, default=30)
    parser.add_argument('--json', default=None)
    parser.add_argument('--only', nargs='+', default=None, help='subset of variant names to run')
    args = parser.parse_args()

    base = Path(args.workdir)

    # generate every variant into its own subdirectory up front so the
    # measurement phase can interleave without regeneration between runs
    selected = {k: v for k, v in VARIANTS.items() if args.only is None or k in args.only}

    mods: dict[str, tuple[Path, str, str, dict]] = {}
    for name, spec in selected.items():
        workdir = base / f'h2h_{name}'
        workdir.mkdir(parents=True, exist_ok=True)
        a, s = _loop.generate_variant(workdir, dict(spec['options']), bool(spec.get('unified', True)))  # type: ignore[arg-type]
        mods[name] = (workdir, a, s, dict(spec.get('env', {})))  # type: ignore[arg-type]
        print(f'generated {name}', flush=True)

    raw: dict[str, list[dict[str, float]]] = {name: [] for name in mods}
    for i in range(args.repeats):
        # alternate direction each pass so no variant systematically runs first
        items = list(mods.items())
        if i % 2:
            items.reverse()
        for name, (workdir, a, s, env) in items:
            m = _loop.measure(workdir, a, s, 1, args.rounds, env)
            raw[name].append(m)
        print(f'pass {i + 1}/{args.repeats} done', flush=True)

    results = {}
    for name, runs in raw.items():
        med = {k: round(statistics.median(r[k] for r in runs), 2) for k in runs[0]}
        results[name] = _loop.cost(med)

    print()
    base_c = results.get('baseline')
    print('| variant | RSS (MB) | import (ms) | connect (ms) | queries (ms) | composite | vs baseline |')
    print('| --- | ---: | ---: | ---: | ---: | ---: | ---: |')
    for name, c in results.items():
        delta = ''
        if base_c and name != 'baseline':
            delta = f'{(c["composite"] / base_c["composite"] - 1) * 100:+.1f}%'
        print(
            f'| {name} | {c["rss_final_mb"]} | {c["import_total_ms"]} '
            f'| {c["connect_total_ms"]} | {c["query_total_ms"]} | {c["composite"]} | {delta} |'
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
        Path(args.json).write_text(json.dumps({'results': results, 'raw': raw}, indent=2))


if __name__ == '__main__':
    sys.exit(main())
