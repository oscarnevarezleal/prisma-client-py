"""Benchmark harness: compare the upstream prisma-client-py generator against
this fork's optimizations (minimal_runtime / separate_model_files /
scalar_fields_only).

It measures three things for each variant on an identical synthetic schema:
  * generated on-disk size of the runtime .py files
  * cold import time of `prisma.types` + `prisma.models`
  * peak process RSS after importing those modules (interpreter baseline subtracted)

Two baselining modes (see --mode):
  * flags    - one interpreter (this fork); baseline = all optimization flags OFF
               (reproduces upstream-equivalent output), optimized = flags ON.
               Fast, isolates exactly the fork's changes.
  * upstream - install the real RobertCraigie/prisma-client-py release in a
               throwaway venv and generate with it. Truest cross-check.
  * both     - run upstream + fork-baseline + fork-optimized.

Example:
    python benchmarks/run.py --models 100 --mode both --repeats 5
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
from dataclasses import dataclass, field, asdict

HERE = Path(__file__).resolve().parent


@dataclass
class VariantResult:
    label: str
    python: str
    flags: dict[str, str]
    generate_ok: bool = False
    import_ok: bool = False
    error: str = ""
    import_error: str = ""
    import_time_s: float = 0.0
    peak_rss_delta_mb: float = 0.0
    size_total_kb: float = 0.0
    size_breakdown_kb: dict[str, float] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# generation
# --------------------------------------------------------------------------- #
def write_schema(models: int, output_pkg: str, schema_path: Path, recursive_type_depth: int) -> None:
    subprocess.run(
        [sys.executable, str(HERE / "gen_schema.py"),
         "--models", str(models), "--output", output_pkg, "--schema", str(schema_path),
         "--recursive-type-depth", str(recursive_type_depth)],
        check=True, capture_output=True, text=True,
    )


def generate(python: str, schema_path: Path, flags: dict[str, str]) -> None:
    # The benchmark only measures generated code size + import cost, never runs
    # queries, so the query-engine binary version is irrelevant. DEBUG_GENERATOR
    # skips the engine-version guard, which otherwise trips when the system
    # Prisma CLI emits a different engine hash than the package pins.
    #
    # CRITICAL: the schema's `provider = "prisma-client-py"` resolves to an
    # *executable on PATH*. To make the correct generator run for each variant
    # (e.g. the upstream venv's generator, not this fork's editable install) we
    # must put the interpreter's own bin/ dir first on PATH.
    # NB: do NOT resolve() — a venv python is a symlink to the system python, and
    # resolving it would point bin_dir at the system bin/ (the wrong generator).
    bin_dir = str(Path(python).absolute().parent)
    # Scrub ambient PRISMA_PY_CONFIG_* so a value set in the caller's shell can't
    # leak into a variant run and skew the comparison (especially upstream, whose
    # flags are {}).
    base_env = {k: v for k, v in os.environ.items() if not k.startswith("PRISMA_PY_CONFIG_")}
    env = {
        **base_env,
        "PRISMA_PY_DEBUG_GENERATOR": "1",
        "PATH": bin_dir + os.pathsep + os.environ.get("PATH", ""),
        **flags,
    }
    subprocess.run(
        [python, "-m", "prisma", "generate", f"--schema={schema_path}"],
        check=True, capture_output=True, text=True, env=env,
    )


# --------------------------------------------------------------------------- #
# measurement
# --------------------------------------------------------------------------- #
class ChildImportError(RuntimeError):
    pass


def _run_child(python: str, parent: str, mods: str) -> dict:
    try:
        out = subprocess.run(
            [python, str(HERE / "_child.py"), parent, mods],
            capture_output=True, text=True, timeout=180,
        )
    except subprocess.TimeoutExpired as exc:
        raise ChildImportError(f"child import timed out after {exc.timeout}s") from exc
    if out.returncode != 0:
        # surface the last meaningful line (e.g. "RecursionError: ...")
        tail = (out.stderr or out.stdout).strip().splitlines()
        msg = next((ln for ln in reversed(tail) if ln.strip() and not ln.startswith(" ")), "")
        raise ChildImportError(msg or "child import failed")
    return json.loads(out.stdout.strip().splitlines()[-1])


def measure_runtime(python: str, parent: Path, repeats: int) -> tuple[float, float]:
    """Return (median import_time_s, peak_rss_delta_mb)."""
    baseline = _run_child(python, "", "")["peak_rss_kb"]
    times: list[float] = []
    peaks: list[int] = []
    for _ in range(repeats):
        r = _run_child(python, str(parent), "prisma.types,prisma.models")
        times.append(r["import_time"])
        peaks.append(r["peak_rss_kb"])
    delta_mb = max(0.0, (statistics.median(peaks) - baseline) / 1024.0)
    return statistics.median(times), delta_mb


def measure_sizes(pkg_dir: Path) -> tuple[float, dict[str, float]]:
    """Total runtime .py size (KB) + per-artifact breakdown."""
    def kb(p: Path) -> float:
        return round(p.stat().st_size / 1024.0, 1) if p.exists() else 0.0

    breakdown = {
        "types.py": kb(pkg_dir / "types.py"),
        "actions.py": kb(pkg_dir / "actions.py"),
        "client.py": kb(pkg_dir / "client.py"),
    }
    # models live either in models.py or a models/ package
    models_py = pkg_dir / "models.py"
    models_dir = pkg_dir / "models"
    if models_dir.is_dir():
        breakdown["models/"] = round(
            sum(f.stat().st_size for f in models_dir.glob("*.py")) / 1024.0, 1
        )
    else:
        breakdown["models.py"] = kb(models_py)

    total = round(sum(f.stat().st_size for f in pkg_dir.rglob("*.py")) / 1024.0, 1)
    return total, breakdown


# --------------------------------------------------------------------------- #
# variants
# --------------------------------------------------------------------------- #
def run_variant(label: str, python: str, flags: dict[str, str],
                models: int, workdir: Path, repeats: int,
                recursive_type_depth: int) -> VariantResult:
    res = VariantResult(label=label, python=python, flags=flags)
    out_root = workdir / label
    if out_root.exists():
        shutil.rmtree(out_root)
    pkg_dir = out_root / "prisma"
    schema_path = out_root / "schema.prisma"
    out_root.mkdir(parents=True, exist_ok=True)

    try:
        write_schema(models, str(pkg_dir), schema_path, recursive_type_depth)
        generate(python, schema_path, flags)
        res.generate_ok = True
    except subprocess.CalledProcessError as exc:
        res.error = (exc.stderr or exc.stdout or str(exc)).strip()[-600:]
        return res

    res.size_total_kb, res.size_breakdown_kb = measure_sizes(pkg_dir)
    try:
        res.import_time_s, res.peak_rss_delta_mb = measure_runtime(python, out_root, repeats)
        res.import_ok = True
    except ChildImportError as exc:
        # e.g. the un-optimized client is too deeply nested to import at scale
        # (Pydantic RecursionError). Keep the size metrics; flag the runtime ones.
        res.import_error = str(exc)
    return res


def setup_upstream_venv(workdir: Path, version: str) -> str:
    venv = workdir / "venv-upstream"
    py = venv / "bin" / "python"
    if not py.exists():
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        subprocess.run([str(py), "-m", "pip", "install", "-q", "--upgrade", "pip"], check=True)
        subprocess.run([str(py), "-m", "pip", "install", "-q", f"prisma=={version}"], check=True)
    return str(py)


FORK_OPTIMIZED_FLAGS = {
    "PRISMA_PY_CONFIG_MINIMAL_RUNTIME": "True",
    "PRISMA_PY_CONFIG_SCALAR_FIELDS_ONLY": "True",
}
FORK_BASELINE_FLAGS = {
    "PRISMA_PY_CONFIG_MINIMAL_RUNTIME": "False",
    "PRISMA_PY_CONFIG_SCALAR_FIELDS_ONLY": "False",
    "PRISMA_PY_CONFIG_SEPARATE_MODEL_FILES": "False",
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--models", type=int, default=50)
    parser.add_argument("--mode", choices=["flags", "upstream", "both"], default="flags")
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--recursive-type-depth", type=int, default=5,
                        help="generator recursive_type_depth applied to every variant "
                             "(-1 = true recursive types, the maintainer's Pyright-only workaround)")
    parser.add_argument("--upstream-version", default="0.15.0")
    parser.add_argument("--separate-model-files", action="store_true",
                        help="also enable separate_model_files in the optimized variant")
    parser.add_argument("--workdir", default="/tmp/prisma_bench")
    parser.add_argument("--json", default="", help="write raw results to this path")
    args = parser.parse_args()

    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    opt_flags = dict(FORK_OPTIMIZED_FLAGS)
    if args.separate_model_files:
        opt_flags["PRISMA_PY_CONFIG_SEPARATE_MODEL_FILES"] = "True"

    variants: list[tuple[str, str, dict[str, str]]] = []
    if args.mode in ("flags", "both"):
        variants.append(("fork-baseline", sys.executable, FORK_BASELINE_FLAGS))
    if args.mode in ("upstream", "both"):
        upstream_py = setup_upstream_venv(workdir, args.upstream_version)
        variants.append((f"upstream-{args.upstream_version}", upstream_py, {}))
    variants.append(("fork-optimized", sys.executable, opt_flags))

    print(f"\nBenchmark: {args.models} models, {args.repeats} repeats, mode={args.mode}, "
          f"recursive_type_depth={args.recursive_type_depth}\n")
    results: list[VariantResult] = []
    for label, python, flags in variants:
        print(f"  running {label} ...", flush=True)
        results.append(run_variant(label, python, flags, args.models, workdir,
                                   args.repeats, args.recursive_type_depth))

    print_report(results, args.models, args.recursive_type_depth)

    if args.json:
        Path(args.json).write_text(json.dumps([asdict(r) for r in results], indent=2))
        print(f"\nRaw results -> {args.json}")


def print_report(results: list[VariantResult], models: int, recursive_type_depth: int) -> None:
    ok = [r for r in results if r.generate_ok]
    failed = [r for r in results if not r.generate_ok]

    print("\n" + "=" * 78)
    print(f"RESULTS  ({models} models, recursive_type_depth={recursive_type_depth})")
    print("=" * 78)
    header = f"{'variant':<22}{'import (ms)':>13}{'peak RSS (MB)':>16}{'runtime .py (KB)':>18}"
    print(header)
    print("-" * 78)
    for r in ok:
        if r.import_ok:
            imp = f"{r.import_time_s * 1000:>13.1f}"
            rss = f"{r.peak_rss_delta_mb:>16.1f}"
        else:
            imp, rss = f"{'unimportable':>13}", f"{'-':>16}"
        print(f"{r.label:<22}{imp}{rss}{r.size_total_kb:>18.1f}")

    for r in ok:
        if not r.import_ok:
            print(f"  ! {r.label} could not be imported: {r.import_error}")

    # relative improvement vs the first successful "baseline-like" variant
    base = next((r for r in ok if "baseline" in r.label or "upstream" in r.label), None)
    opt = next((r for r in ok if r.label == "fork-optimized"), None)
    if base and opt:
        print("-" * 78)
        print(f"\nImprovement  (fork-optimized vs {base.label}):")
        if base.import_ok and opt.import_ok:
            _pct("import time", base.import_time_s, opt.import_time_s)
            _pct("peak RSS", base.peak_rss_delta_mb, opt.peak_rss_delta_mb)
        else:
            print(f"  import time / peak RSS   n/a ({base.label} is unimportable at this scale;")
            print(f"                           fork-optimized imports in {opt.import_time_s * 1000:.0f} ms)")
        _pct("runtime .py size", base.size_total_kb, opt.size_total_kb)
        print("\nPer-artifact size (KB):")
        keys = sorted({k for r in ok for k in r.size_breakdown_kb})
        print(f"  {'artifact':<14}" + "".join(f"{r.label:>22}" for r in ok))
        for k in keys:
            row = "".join(f"{r.size_breakdown_kb.get(k, 0.0):>22.1f}" for r in ok)
            print(f"  {k:<14}{row}")

    for r in failed:
        print(f"\n[FAILED] {r.label}:\n{r.error}")


def _pct(label: str, base: float, opt: float) -> None:
    if base <= 0:
        print(f"  {label:<18} n/a")
        return
    pct = (base - opt) / base * 100.0
    print(f"  {label:<18} {base:>10.1f} -> {opt:>10.1f}   ({pct:+.1f}%)")


if __name__ == "__main__":
    main()
