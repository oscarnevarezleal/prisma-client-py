"""Autonomous optimization loop for a dual (sync + async) client against live PG.

Iterates a ladder of candidate changes. Each iteration:

  1. regenerate both clients with the candidate's generator options
     (optionally grafting them into one unified package)
  2. warm bytecode caches, then run workload.py in fresh processes N times
  3. score the medians; ACCEPT the candidate if the composite cost improves
     by >= --threshold (default 0.5%) vs the current best *and* query latency
     does not regress beyond the guardrail; otherwise REJECT and revert

Stops when no remaining candidate clears the threshold (diminishing returns).
Candidates that fail to generate or crash the workload are auto-rejected —
that is how behavior-changing options (e.g. scalarFieldsOnly under an
include-heavy workload) get filtered out instead of silently shipping.

Composite cost = geometric mean of:
  rss_final_mb            retained python RSS with both clients live
  import_total_ms         import of both client modules (warm bytecode)
  connect_total_ms        both engine spawns + connects
  query_total_ms          sum of per-shape median latencies, both flavours

Usage:
  BENCH_DATABASE_URL=postgresql://bench:bench@127.0.0.1:5433/bench \
      python optimize_loop.py --workdir /tmp/pglab --repeats 3
"""

from __future__ import annotations

import os
import sys
import json
import shutil
import argparse
import subprocess
import statistics
from pathlib import Path

HERE = Path(__file__).resolve().parent

QUERY_KEYS = [
    'async_q_find_many_include_ms',
    'async_q_find_unique_ms',
    'async_q_count_ms',
    'async_q_create_delete_ms',
    'async_q_query_raw_ms',
    'sync_q_find_many_include_ms',
    'sync_q_find_unique_ms',
    'sync_q_count_ms',
    'sync_q_create_delete_ms',
    'sync_q_query_raw_ms',
]

# Phase 2: foundational mechanisms implemented in the fork (no upstream
# equivalent), tried on top of the phase-1 winner. 'env' entries are runtime
# switches passed to the workload process; 'options' are generator options.
CANDIDATES_PHASE2: list[dict[str, object]] = [
    {
        'name': 'lazy-actions',
        'options': {'lazyActions': 'true'},
        'why': 'action namespaces + actions module import deferred to first DB access',
    },
    {
        'name': 'shared-engine',
        'env': {'PRISMA_PY_SHARED_ENGINE': '1'},
        'why': 'sync+async clients share one query-engine process (registry, refcounted)',
    },
    {
        'name': 'fast-parse',
        'env': {'PRISMA_PY_FAST_PARSE': '1'},
        'why': 'trusted engine responses: compiled converters + model_construct, no validation pass',
    },
    {
        'name': 'slim-models',
        'options': {'modelBackend': '"slim"'},
        'why': 'pydantic-free __slots__ records; no core-schema compilation at all',
    },
]

# The ladder. Options accumulate: each candidate is (name, extra options, unified?)
# and is tried on top of the current best configuration.
CANDIDATES: list[dict[str, object]] = [
    {
        'name': 'recursive-types',
        'options': {'recursive_type_depth': '-1'},
        'why': 'true-recursive types shrink types.py ~4x; maintainer-recommended for big schemas',
    },
    {
        'name': 'minimal-runtime',
        'options': {'minimalRuntime': 'true'},
        'why': 'move query-arg types into .pyi stubs; slim runtime dicts',
    },
    {
        'name': 'unified-package',
        'unified': True,
        'why': 'share models/types between interfaces; only actions/client duplicated',
    },
    {
        'name': 'separate-model-files',
        'options': {'separateModelFiles': 'true'},
        'why': 'lazy per-model files: pay only for models actually touched',
    },
    {
        'name': 'recursive-validation-models',
        'options': {'recursiveValidationModels': 'true'},
        'why': 'defer_build: compile validators on first use instead of import',
    },
    {
        'name': 'scalar-fields-only',
        'options': {'scalarFieldsOnly': 'true'},
        'why': 'drop relation fields from models (BEHAVIOR CHANGE - expect workload rejection)',
    },
]


def run(cmd: list[str], cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, env=env or os.environ.copy(), capture_output=True, text=True, check=True)


def generate_variant(workdir: Path, options: dict[str, str], unified: bool) -> tuple[str, str]:
    """(Re)generate both clients under `options`; return the two client module specs."""
    env = os.environ.copy()
    for name, iface, pkg in (('async', 'asyncio', 'pkg_async'), ('sync', 'sync', 'pkg_sync')):
        for stale in (workdir / pkg, workdir / 'pkg_uni'):
            if stale.exists():
                shutil.rmtree(stale)
        opt_args: list[str] = []
        for k, v in options.items():
            opt_args += ['--option', f'{k}={v}']
        run(
            [sys.executable, str(HERE / 'schema_gen.py'), '--interface', iface,
             '--output', f'./{pkg}', '--schema', f'./{name}.prisma', *opt_args],
            cwd=workdir,
        )
        run([sys.executable, '-m', 'prisma', 'generate', f'--schema=./{name}.prisma'], cwd=workdir, env=env)

    if not unified:
        return 'pkg_async.client', 'pkg_sync.client'

    # graft: one package, shared models/types, two action+client layers
    uni = workdir / 'pkg_uni'
    shutil.copytree(workdir / 'pkg_async', uni, ignore=shutil.ignore_patterns('__pycache__'))
    actions = (workdir / 'pkg_sync' / 'actions.py').read_text().replace(
        'from .client import Prisma', 'from .client_sync import Prisma'
    )
    client = (
        (workdir / 'pkg_sync' / 'client.py')
        .read_text()
        # eager layout
        .replace(
            'from . import types, models, errors, actions',
            'from . import types, models, errors\nfrom . import actions_sync as actions',
        )
        # lazyActions layout: the deferred import inside Prisma.__getattr__
        .replace('from . import actions as _actions', 'from . import actions_sync as _actions')
    )
    (uni / 'actions_sync.py').write_text(actions)
    (uni / 'client_sync.py').write_text(client)
    return 'pkg_uni.client', 'pkg_uni.client_sync'


def measure(workdir: Path, async_mod: str, sync_mod: str, repeats: int, rounds: int,
            extra_env: dict[str, str] | None = None) -> dict[str, float]:
    """Median of `repeats` fresh-process workload runs (bytecode pre-warmed)."""
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    run([sys.executable, '-c', f'import {async_mod}, {sync_mod}'], cwd=workdir, env=env)
    runs: list[dict[str, float]] = []
    for _ in range(repeats):
        proc = run(
            [sys.executable, str(HERE / 'workload.py'), async_mod, sync_mod, '--rounds', str(rounds)],
            cwd=workdir, env=env,
        )
        runs.append(json.loads(proc.stdout.strip().splitlines()[-1]))
    return {k: round(statistics.median(r[k] for r in runs), 2) for k in runs[0]}


def cost(m: dict[str, float]) -> dict[str, float]:
    rss = m['rss_final_mb']
    imp = m['import_async_ms'] + m['import_sync_ms']
    con = m['async_connect_ms'] + m['sync_connect_ms']
    qry = sum(m[k] for k in QUERY_KEYS)
    composite = (rss * imp * con * qry) ** 0.25
    return {
        'rss_final_mb': rss,
        'import_total_ms': round(imp, 1),
        'connect_total_ms': round(con, 1),
        'query_total_ms': round(qry, 2),
        'composite': round(composite, 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--workdir', required=True)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--rounds', type=int, default=20)
    parser.add_argument('--threshold', type=float, default=0.005, help='min relative improvement to accept')
    parser.add_argument('--latency-guardrail', type=float, default=0.10,
                        help='max tolerated query_total_ms regression')
    parser.add_argument('--json', default=None)
    parser.add_argument('--phase', type=int, default=1, choices=(1, 2),
                        help='1: generator-option ladder from scratch; '
                             '2: foundational-mechanism ladder on top of the phase-1 winner')
    args = parser.parse_args()

    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    assert os.environ.get('BENCH_DATABASE_URL'), 'BENCH_DATABASE_URL must be set'

    history: list[dict[str, object]] = []

    def record(name: str, options: dict[str, str], unified: bool, verdict: str,
               scores: dict[str, float] | None, note: str = '') -> None:
        entry: dict[str, object] = {'iteration': len(history), 'name': name, 'verdict': verdict,
                                    'options': dict(options), 'unified': unified, 'note': note}
        if scores:
            entry.update(scores)
        history.append(entry)
        print(json.dumps(entry), flush=True)

    # iteration 0: baseline
    if args.phase == 2:
        # phase-1 winner is the new baseline
        best_options: dict[str, str] = {
            'recursive_type_depth': '-1',
            'minimalRuntime': 'true',
            'separateModelFiles': 'true',
        }
        best_unified = True
    else:
        best_options = {}
        best_unified = False
    best_env: dict[str, str] = {}
    a, s = generate_variant(workdir, best_options, best_unified)
    best = cost(measure(workdir, a, s, args.repeats, args.rounds, best_env))
    record('baseline', best_options, best_unified, 'ACCEPT', best)

    remaining = list(CANDIDATES_PHASE2 if args.phase == 2 else CANDIDATES)
    while remaining:
        candidate = remaining.pop(0)
        name = str(candidate['name'])
        trial_options = {**best_options, **candidate.get('options', {})}  # type: ignore[arg-type]
        trial_env = {**best_env, **candidate.get('env', {})}  # type: ignore[arg-type]
        trial_unified = bool(candidate.get('unified', best_unified)) or best_unified

        try:
            a, s = generate_variant(workdir, trial_options, trial_unified)
            scores = cost(measure(workdir, a, s, args.repeats, args.rounds, trial_env))
        except subprocess.CalledProcessError as exc:
            record(name, trial_options, trial_unified, 'REJECT', None,
                   note=f'failed: {(exc.stderr or exc.stdout or "")[-300:]}')
            continue

        improvement = 1 - scores['composite'] / best['composite']
        latency_delta = scores['query_total_ms'] / best['query_total_ms'] - 1
        if improvement >= args.threshold and latency_delta <= args.latency_guardrail:
            note = f'composite -{improvement * 100:.1f}%'
            best, best_options, best_env, best_unified = scores, trial_options, trial_env, trial_unified
            record(name, trial_options, trial_unified, 'ACCEPT', scores, note)
        else:
            reason = (f'improvement {improvement * 100:+.2f}% < threshold'
                      if latency_delta <= args.latency_guardrail
                      else f'latency regression {latency_delta * 100:+.1f}%')
            record(name, trial_options, trial_unified, 'REJECT', scores, reason)

    # regenerate the winning configuration so the workdir ends on the best state
    generate_variant(workdir, best_options, best_unified)
    summary = {'final_options': best_options, 'final_env': best_env, 'unified': best_unified, 'final_scores': best}
    print(json.dumps({'DONE': summary}), flush=True)

    if args.json:
        Path(args.json).write_text(json.dumps({'history': history, 'summary': summary}, indent=2))


if __name__ == '__main__':
    main()
