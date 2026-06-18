"""SPIKE 2 (throwaway de-risking experiment, not production code).

Targets the two biggest open risks for the B+C "true-recursive, lazily-built
validation models" plan:

  PART 1 - Nested-relation validation correctness.
      Does validating a model with a *nested relation payload* actually validate
      the nested data (good accepted as real model instances; bad rejected with a
      correct error location)? And how deep can data nest before it becomes a
      data-shaped (not schema-shaped) recursion problem?

  PART 2 - A derived, BOUNDED recursion-limit policy + safe execution.
      A blanket sys.setrecursionlimit(20000) is unsafe (too high -> C-stack
      overflow -> segfault). Instead:
        (a) empirically measure how the *minimum* recursion limit needed to BUILD
            the models scales with relation-chain depth -> frames-per-level,
        (b) turn that into a formula the generator could emit from the schema's
            known max relation depth,
        (c) show the segfault-safe way to honour it: build inside a worker thread
            with an enlarged stack, so deep-but-bounded schemas work reliably and
            an under-provisioned limit fails *catchably* rather than crashing.

Run:  python benchmarks/spike_nested_and_recursion_policy.py
"""
from __future__ import annotations

import sys
import json
import tempfile
import textwrap
import subprocess
from pathlib import Path

SCALARS = [("f_str", "str"), ("f_int", "int"), ("f_bool", "bool"), ("f_float", "float")]


def gen_module(n: int) -> str:
    """N chained recursive models, all defer_build=True (lazy)."""
    out = [
        "from __future__ import annotations",
        "from typing import Optional, List",
        "from pydantic import BaseModel, ConfigDict",
        "",
    ]
    for i in range(n):
        out.append(f"class Model{i}(BaseModel):")
        out.append("    model_config = ConfigDict(defer_build=True)")
        out.append("    id: int")
        for name, typ in SCALARS:
            out.append(f"    {name}: {typ}")
        if i < n - 1:
            out.append(f"    next: Optional[Model{i + 1}] = None")
        if i > 0:
            out.append(f"    prev: Optional[List[Model{i - 1}]] = None")
        out.append("")
    return "\n".join(out) + "\n"


def _write(n: int) -> Path:
    d = Path(tempfile.mkdtemp(prefix="spike2_"))
    (d / "spikemod.py").write_text(gen_module(n))
    return d


# --------------------------------------------------------------------------- #
# PART 1 - nested-relation validation
# --------------------------------------------------------------------------- #
NESTED_DRIVER = textwrap.dedent(
    """
    import sys, json, threading
    moddir, n, data_depth, limit, stack_mb = sys.argv[1:6]
    n, data_depth, limit, stack_mb = int(n), int(data_depth), int(limit), int(stack_mb)
    sys.path.insert(0, moddir)
    res = {"ok": False, "error": "", "good_typed": False, "bad_loc": ""}

    def good(depth):
        root = {}; cur = root
        for k in range(depth):
            cur.update(id=k, f_str="x", f_int=k, f_bool=True, f_float=1.0)
            if k < depth - 1:
                cur["next"] = {}; cur = cur["next"]
        return root

    def bad(depth, at):
        d = good(depth); cur = d
        for _ in range(at):
            cur = cur["next"]
        cur["f_int"] = "NOT-AN-INT"
        return d

    def work():
        import importlib
        m = importlib.import_module("spikemod")
        Model0 = m.Model0
        inst = Model0.model_validate(good(data_depth))
        # walk the nested instances and confirm they are real model objects
        cur, ok = inst, True
        for k in range(data_depth):
            ok = ok and type(cur).__name__ == f"Model{k}" and cur.id == k
            if k < data_depth - 1:          # Model{data_depth-1} has no `next`
                cur = cur.next
        res["good_typed"] = ok
        # bad data a few levels down -> must raise with a nested loc
        try:
            Model0.model_validate(bad(data_depth, min(3, data_depth - 1)))
            res["error"] = "bad data NOT rejected"
        except Exception as e:
            if type(e).__name__ == "ValidationError":
                loc = e.errors()[0]["loc"]
                res["bad_loc"] = ".".join(str(x) for x in loc)
                res["ok"] = res["good_typed"]
            else:
                res["error"] = f"wrong exc {type(e).__name__}"

    def runner():
        # capture exceptions HERE so they're recorded even when run in a thread
        # (a thread's exception does not propagate to join()).
        try:
            work()
        except RecursionError:
            res["error"] = "RecursionError"
        except Exception as e:
            res["error"] = f"{type(e).__name__}: {e}"[:120]

    sys.setrecursionlimit(limit)
    if stack_mb:
        threading.stack_size(stack_mb * 1024 * 1024)
        t = threading.Thread(target=runner); t.start(); t.join()
    else:
        runner()
    print(json.dumps(res))
    """
)


def run_nested(n: int, data_depth: int, limit: int, stack_mb: int) -> dict:
    d = _write(n)
    drv = d / "_drv.py"; drv.write_text(NESTED_DRIVER)
    out = subprocess.run([sys.executable, str(drv), str(d), str(n), str(data_depth),
                          str(limit), str(stack_mb)], capture_output=True, text=True)
    try:
        return json.loads(out.stdout.strip().splitlines()[-1])
    except Exception:
        rc = out.returncode
        return {"ok": False, "error": f"crashed rc={rc} (likely segfault)" if rc < 0 else (out.stderr[-120:])}


# --------------------------------------------------------------------------- #
# PART 2 - minimum build recursion limit vs chain depth
# --------------------------------------------------------------------------- #
BUILD_DRIVER = textwrap.dedent(
    """
    import sys, importlib
    moddir, limit = sys.argv[1], int(sys.argv[2])
    sys.path.insert(0, moddir)
    sys.setrecursionlimit(limit)
    try:
        m = importlib.import_module("spikemod")
        m.Model0(id=0, f_str="x", f_int=0, f_bool=True, f_float=1.0)  # force build
        sys.exit(0)
    except RecursionError:
        sys.exit(7)
    """
)


def build_feasible(moddir: Path, limit: int) -> bool:
    drv = moddir / "_b.py"; drv.write_text(BUILD_DRIVER)
    rc = subprocess.run([sys.executable, str(drv), str(moddir), str(limit)],
                        capture_output=True, text=True).returncode
    return rc == 0


def min_build_limit(n: int, hi: int = 30000) -> int:
    """Smallest recursion limit that lets Model0 build (binary search)."""
    d = _write(n)
    lo = 50
    if not build_feasible(d, hi):
        return -1
    while lo < hi:
        mid = (lo + hi) // 2
        if build_feasible(d, mid):
            hi = mid
        else:
            lo = mid + 1
    return lo


# --------------------------------------------------------------------------- #
def main() -> None:
    print(f"\nSPIKE 2  (pydantic {__import__('pydantic').VERSION}, python {sys.version.split()[0]}, "
          f"default recursionlimit={sys.getrecursionlimit()})\n")

    # ---- PART 1 -----------------------------------------------------------
    print("PART 1 - nested-relation validation (schema=120 models, lazy)")
    print(f"   {'data depth':>11} {'limit':>7} {'stack':>7}  result")
    for depth, limit, stack in [(5, 2000, 0), (20, 4000, 0), (60, 12000, 64), (120, 30000, 256)]:
        r = run_nested(120, depth, limit, stack)
        verdict = (f"OK  nested validated; bad rejected at loc='{r['bad_loc']}'"
                   if r.get("ok") else f"FAILED: {r.get('error') or 'good_typed=%s' % r.get('good_typed')}")
        print(f"   {depth:>11} {limit:>7} {(str(stack)+'MB') if stack else '-':>7}  {verdict}")

    # ---- PART 2 -----------------------------------------------------------
    print("\nPART 2 - minimum recursion limit to BUILD vs relation-chain depth")
    print(f"   {'chain depth (N)':>16} {'min limit':>10} {'frames/level':>13}")
    pts = []
    for n in [20, 50, 100, 150, 200]:
        lim = min_build_limit(n)
        fpl = (lim / n) if lim > 0 else float("nan")
        pts.append((n, lim))
        print(f"   {n:>16} {lim:>10} {fpl:>13.1f}")

    # linear fit min_limit ~= slope*N + intercept
    (n0, l0), (n1, l1) = pts[0], pts[-1]
    slope = (l1 - l0) / (n1 - n0)
    intercept = l0 - slope * n0
    print(f"\n   fit: min_build_limit ≈ {slope:.1f} * max_chain_depth + {intercept:.0f}")
    print("   DERIVED POLICY (generator knows max relation-chain depth D at gen time):")
    print(f"     limit = current_limit + ceil({slope:.0f} * D * SAFETY) + HEADROOM")
    D = 200
    recommended = int((slope * 1.5) * D + 2000)
    print(f"     e.g. D={D}, SAFETY=1.5, HEADROOM=2000  ->  limit={recommended}")

    # ---- PART 2b: the derived limit works AND too-low fails catchably ------
    print("\nPART 2b - validate the policy at D=200 (built in a worker thread w/ enlarged stack)")
    enough = run_nested(200, 5, recommended, 256)
    toolow = run_nested(200, 5, 800, 256)   # under-provisioned limit
    print(f"   derived limit ({recommended}):  {'OK (builds + validates)' if enough.get('ok') else 'FAILED: '+str(enough.get('error'))}")
    print(f"   under-limit  (800):          {'catchable RecursionError' if toolow.get('error')=='RecursionError' else 'unexpected: '+str(toolow.get('error'))}")


if __name__ == "__main__":
    main()
