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
      python optimize_loop.py --workdir /tmp/pglab --repeats 3 --json phase1.json

  # phase 2 starts from the configuration phase 1 actually settled on, read
  # out of that run's result file rather than hardcoded here
  BENCH_DATABASE_URL=... python optimize_loop.py --workdir /tmp/pglab \
      --phase 2 --phase1-json phase1.json --json phase2.json
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

# Every metric `cost()` reads. A workload run that does not report all of them
# is a failed measurement, not a fast one.
REQUIRED_KEYS = [
    'rss_final_mb',
    'import_async_ms',
    'import_sync_ms',
    'async_connect_ms',
    'sync_connect_ms',
    *QUERY_KEYS,
]


class MeasurementError(RuntimeError):
    """A workload process ran but produced no usable measurement."""


class GraftError(RuntimeError):
    """The unified-package graft did not find the generated source it rewrites."""


def positive_int(value: str) -> int:
    """argparse type: reject 0 and negatives, which produce empty run lists."""
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError(f'must be >= 1, got {parsed}')
    return parsed


# Phase 2: foundational mechanisms implemented in the fork (no upstream
# equivalent), tried on top of the phase-1 winner. 'env' entries are runtime
# switches passed to the workload process; 'options' are generator options.
#
# NOTE: the `msgspec-models` rung was added after the recorded run in
# `results-phase2-2026-08-03.json`, which therefore has four rungs, not five.
# See the phase-2 section of README.md.
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
    {
        'name': 'msgspec-models',
        # msgspec resolves cyclic relation refs against one module, so this
        # candidate also turns separateModelFiles back off
        'options': {'modelBackend': '"msgspec"', 'separateModelFiles': 'false'},
        'why': 'C-decoded msgspec structs; 5-9x faster deserialization in microbenchmarks',
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
    (uni / 'actions_sync.py').write_text(graft_actions((workdir / 'pkg_sync' / 'actions.py').read_text()))
    (uni / 'client_sync.py').write_text(graft_client((workdir / 'pkg_sync' / 'client.py').read_text()))
    return 'pkg_uni.client', 'pkg_uni.client_sync'


# The graft rewrites exact lines of generated source, and later ladder
# candidates change that source (`lazyActions` moves the actions import into
# `Prisma.__getattr__`). `str.replace` returns its input unchanged when the
# pattern is absent, so an unmatched pattern would leave the *sync* client
# importing the *async* actions layer — a package that still imports and still
# runs, but is no longer the configuration under test. Every rewrite below is
# therefore checked, and a miss aborts the run instead of quietly moving the
# baseline.

_ACTIONS_CLIENT_IMPORT = 'from .client import Prisma'
_CLIENT_EAGER_IMPORT = 'from . import types, models, errors, actions'
_CLIENT_LAZY_IMPORT = 'from . import actions as _actions'


def graft_actions(source: str) -> str:
    if _ACTIONS_CLIENT_IMPORT not in source:
        raise GraftError(f'pkg_sync/actions.py: no {_ACTIONS_CLIENT_IMPORT!r} to rewrite')
    return source.replace(_ACTIONS_CLIENT_IMPORT, 'from .client_sync import Prisma')


def graft_client(source: str) -> str:
    """Point the sync client at `actions_sync`, under either actions layout."""
    eager = _CLIENT_EAGER_IMPORT in source
    lazy = _CLIENT_LAZY_IMPORT in source
    if eager == lazy:
        raise GraftError(
            'pkg_sync/client.py: expected exactly one of the eager and lazyActions import layouts, '
            f'found eager={eager} lazy={lazy} — the client template has changed under the graft'
        )
    if eager:
        return source.replace(
            _CLIENT_EAGER_IMPORT,
            'from . import types, models, errors\nfrom . import actions_sync as actions',
        )
    # lazyActions layout: the deferred import inside Prisma.__getattr__
    return source.replace(_CLIENT_LAZY_IMPORT, 'from . import actions_sync as _actions')


def parse_workload_output(stdout: str) -> dict[str, float]:
    """Last stdout line -> metrics, or `MeasurementError`.

    A workload can exit 0 and still be useless: crash after printing, print a
    traceback, print nothing. Left unchecked those surface as `IndexError` /
    `JSONDecodeError` / `KeyError` from outside the loop's `CalledProcessError`
    handler and abort the whole optimization run.
    """
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        raise MeasurementError('workload wrote nothing to stdout')
    try:
        parsed = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise MeasurementError(f'last stdout line is not JSON ({exc}): {lines[-1][:200]!r}') from exc
    if not isinstance(parsed, dict):
        raise MeasurementError(f'workload emitted {type(parsed).__name__}, expected a JSON object')

    missing = [k for k in REQUIRED_KEYS if k not in parsed]
    if missing:
        raise MeasurementError(f'workload output is missing {len(missing)} metric(s): {", ".join(missing)}')
    bad = [k for k in REQUIRED_KEYS if isinstance(parsed[k], bool) or not isinstance(parsed[k], (int, float))]
    if bad:
        raise MeasurementError(f'workload reported non-numeric metric(s): {", ".join(bad)}')
    return {k: float(v) for k, v in parsed.items() if not isinstance(v, bool) and isinstance(v, (int, float))}


def measure(workdir: Path, async_mod: str, sync_mod: str, repeats: int, rounds: int,
            extra_env: dict[str, str] | None = None) -> dict[str, float]:
    """Median of `repeats` fresh-process workload runs (bytecode pre-warmed)."""
    if repeats < 1:
        raise MeasurementError(f'repeats must be >= 1, got {repeats}')
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
        runs.append(parse_workload_output(proc.stdout))
    # only keys every run reported; REQUIRED_KEYS are guaranteed to be among them
    shared = [k for k in runs[0] if all(k in r for r in runs)]
    return {k: round(statistics.median(r[k] for r in runs), 2) for k in shared}


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


def load_phase1_winner(path: Path) -> tuple[dict[str, str], dict[str, str], bool]:
    """Read the configuration a phase-1 run actually settled on.

    Phase 2 measures mechanisms *on top of* the phase-1 winner, so it has to
    start from the winner that run produced. Restating it here would go stale
    the moment the ladder, the threshold or the guardrail changes, and phase 2
    would then report deltas against a baseline no phase-1 run ever chose.
    """
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f'--phase1-json {path}: cannot read ({exc})') from exc
    summary = data.get('summary') if isinstance(data, dict) else None
    if not isinstance(summary, dict) or 'final_options' not in summary or 'unified' not in summary:
        raise SystemExit(
            f'--phase1-json {path}: no usable "summary" object — expected the --json output of a '
            'phase-1 optimize_loop run'
        )
    options = {str(k): str(v) for k, v in dict(summary['final_options']).items()}
    # phase-1 result files written before `final_env` existed simply have none
    env = {str(k): str(v) for k, v in dict(summary.get('final_env') or {}).items()}
    return options, env, bool(summary['unified'])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--workdir', required=True)
    parser.add_argument('--repeats', type=positive_int, default=3)
    parser.add_argument('--rounds', type=positive_int, default=20)
    parser.add_argument('--threshold', type=float, default=0.005, help='min relative improvement to accept')
    parser.add_argument('--latency-guardrail', type=float, default=0.10,
                        help='max tolerated query_total_ms regression')
    parser.add_argument('--json', default=None)
    parser.add_argument('--phase', type=int, default=1, choices=(1, 2),
                        help='1: generator-option ladder from scratch; '
                             '2: foundational-mechanism ladder on top of the phase-1 winner')
    parser.add_argument('--phase1-json', default=None,
                        help='result file of the phase-1 run whose winner phase 2 builds on '
                             '(required with --phase 2)')
    args = parser.parse_args()
    if args.phase == 2 and not args.phase1_json:
        parser.error('--phase 2 requires --phase1-json: the phase-2 baseline is the winner of a real phase-1 run')
    if args.phase == 1 and args.phase1_json:
        parser.error('--phase1-json only applies to --phase 2')

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
    best_options: dict[str, str]
    best_env: dict[str, str]
    if args.phase == 2:
        # the phase-1 winner is the new baseline — read from that run, not restated
        best_options, best_env, best_unified = load_phase1_winner(Path(args.phase1_json))
        print(json.dumps({'phase1_winner': {'options': best_options, 'env': best_env, 'unified': best_unified},
                          'from': args.phase1_json}), flush=True)
    else:
        best_options = {}
        best_env = {}
        best_unified = False
    a, s = generate_variant(workdir, best_options, best_unified)
    try:
        best = cost(measure(workdir, a, s, args.repeats, args.rounds, best_env))
    except MeasurementError as exc:
        # nothing to compare candidates against; there is no run to salvage
        raise SystemExit(f'baseline measurement failed: {exc}') from exc
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
        except MeasurementError as exc:
            # ran, exited 0, produced nothing usable: still this candidate's
            # problem, not a reason to abandon the remaining ladder
            record(name, trial_options, trial_unified, 'REJECT', None, note=f'unusable measurement: {exc}')
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
