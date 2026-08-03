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

VARIANTS: dict[str, dict[str, object]] = {
    'pydantic': {
        'options': {'recursive_type_depth': '-1', 'minimalRuntime': 'true', 'separateModelFiles': 'true'},
    },
    'slim': {
        'options': {
            'recursive_type_depth': '-1',
            'minimalRuntime': 'true',
            'separateModelFiles': 'true',
            'lazyActions': 'true',
            'modelBackend': '"slim"',
        },
    },
    'msgspec': {
        'options': {
            'recursive_type_depth': '-1',
            'minimalRuntime': 'true',
            'separateModelFiles': 'false',
            'lazyActions': 'true',
            'modelBackend': '"msgspec"',
        },
    },
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workdir', required=True)
    parser.add_argument('--repeats', type=int, default=7)
    parser.add_argument('--rounds', type=int, default=30)
    parser.add_argument('--json', default=None)
    args = parser.parse_args()

    base = Path(args.workdir)

    # generate every variant into its own subdirectory up front so the
    # measurement phase can interleave without regeneration between runs
    mods: dict[str, tuple[Path, str, str]] = {}
    for name, spec in VARIANTS.items():
        workdir = base / f'h2h_{name}'
        workdir.mkdir(parents=True, exist_ok=True)
        a, s = _loop.generate_variant(workdir, dict(spec['options']), True)  # type: ignore[arg-type]
        mods[name] = (workdir, a, s)
        print(f'generated {name}', flush=True)

    raw: dict[str, list[dict[str, float]]] = {name: [] for name in VARIANTS}
    for i in range(args.repeats):
        for name, (workdir, a, s) in mods.items():
            m = _loop.measure(workdir, a, s, 1, args.rounds)
            raw[name].append(m)
        print(f'pass {i + 1}/{args.repeats} done', flush=True)

    results = {}
    for name, runs in raw.items():
        med = {k: round(statistics.median(r[k] for r in runs), 2) for k in runs[0]}
        results[name] = _loop.cost(med)

    print()
    print('| variant | RSS (MB) | import (ms) | connect (ms) | queries (ms) | composite |')
    print('| --- | ---: | ---: | ---: | ---: | ---: |')
    for name, c in results.items():
        print(
            f'| {name} | {c["rss_final_mb"]} | {c["import_total_ms"]} '
            f'| {c["connect_total_ms"]} | {c["query_total_ms"]} | {c["composite"]} |'
        )

    if args.json:
        Path(args.json).write_text(json.dumps({'results': results, 'raw': raw}, indent=2))


if __name__ == '__main__':
    sys.exit(main())
