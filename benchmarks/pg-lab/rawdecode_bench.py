"""Interleaved A/B of PRISMA_PY_RAW_DECODE across result-set sizes.

One-pass decoding removes the `bytes -> dict -> records` hop, so its benefit is
proportional to how much JSON a query returns. Sequential A-then-B runs are too
noisy to resolve a few percent (two back-to-back runs disagreed by 13 points),
so this alternates A,B,A,B,... in fresh processes and takes medians.

    python rawdecode_bench.py --workdir /tmp/pglab/msmoke --passes 9
"""

from __future__ import annotations

import os
import sys
import json
import argparse
import subprocess
import statistics
from pathlib import Path

HERE = Path(__file__).resolve().parent

CHILD = r'''
import asyncio, json, statistics, sys, time
sys.path.insert(0, ".")
from pkg.client import Prisma

CASES = [(1, None), (25, None), (25, "inc"), (200, None), (400, None), (400, "inc")]
INC = {"author": True, "comments": True}

async def main():
    db = Prisma(); await db.connect()
    out = {}
    for take, inc in CASES:
        kw = {"take": take}
        if inc: kw["include"] = INC
        await db.post.find_many(**kw)
        n = 30 if take <= 200 else 15
        ts = []
        for _ in range(n):
            t = time.perf_counter(); await db.post.find_many(**kw); ts.append(time.perf_counter() - t)
        out[f"{take}{'+inc' if inc else ''}"] = statistics.median(ts) * 1000
    await db.disconnect()
    print(json.dumps(out))

asyncio.run(main())
'''


def positive_int(value: str) -> int:
    """argparse type: reject 0 and negatives, which leave the run lists empty."""
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError(f'must be >= 1, got {parsed}')
    return parsed


def run(workdir: Path, raw: bool) -> dict[str, float]:
    env = os.environ.copy()
    if raw:
        env['PRISMA_PY_RAW_DECODE'] = '1'
    else:
        env.pop('PRISMA_PY_RAW_DECODE', None)
    proc = subprocess.run(
        [sys.executable, '-c', CHILD], cwd=workdir, env=env, capture_output=True, text=True, check=True
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workdir', required=True, help='dir containing the generated `pkg` package')
    parser.add_argument('--passes', type=positive_int, default=9)
    parser.add_argument('--json', default=None)
    args = parser.parse_args()

    workdir = Path(args.workdir)
    dict_runs: list[dict[str, float]] = []
    raw_runs: list[dict[str, float]] = []
    for i in range(args.passes):
        # alternate the order too, so neither variant systematically runs on a
        # warmer machine than the other
        if i % 2 == 0:
            dict_runs.append(run(workdir, False))
            raw_runs.append(run(workdir, True))
        else:
            raw_runs.append(run(workdir, True))
            dict_runs.append(run(workdir, False))
        print(f'pass {i + 1}/{args.passes}', flush=True)

    keys = list(dict_runs[0])
    print()
    print('| result set | dict path | one-pass decode | change |')
    print('| --- | ---: | ---: | ---: |')
    results = {}
    for k in keys:
        a = statistics.median(r[k] for r in dict_runs)
        b = statistics.median(r[k] for r in raw_runs)
        results[k] = {'dict_ms': round(a, 3), 'raw_ms': round(b, 3), 'pct': round((b - a) / a * 100, 1)}
        print(f'| {k} | {a:.2f} ms | {b:.2f} ms | {(b - a) / a * 100:+.1f}% |')

    if args.json:
        Path(args.json).write_text(json.dumps({'summary': results, 'dict': dict_runs, 'raw': raw_runs}, indent=2))


if __name__ == '__main__':
    main()
