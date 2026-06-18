"""SPIKE (throwaway de-risking experiment, not production code).

Question this answers
---------------------
Option B+C proposes replacing the generator's depth-duplicated record types with
*true-recursive* Pydantic v2 models, built *lazily*, so we get full runtime
validation AND flat memory AND no import-time RecursionError on large cyclic
schemas. Before investing, we need to know empirically:

  1. Does eagerly building N chained recursive models reproduce the RecursionError
     we saw in the real client? (baseline / current behavior)
  2. Does merely raising the recursion limit fix it, and at what cost?
  3. With `defer_build=True`, does validating a SINGLE model avoid building the
     whole transitive chain -- i.e. does cost scale with *use*, not schema size?
  4. Is record validation actually intact in the lazy case?

It hand-rolls a synthetic module of N mutually-recursive models matching the
benchmark's worst-case chain topology (Model0 -> Model1 -> ... -> ModelN), then
measures each strategy in a fresh subprocess.

Run:  python benchmarks/spike_recursive_models.py
"""
from __future__ import annotations

import os
import sys
import json
import tempfile
import textwrap
import subprocess
from pathlib import Path

SCALARS = [("f_str", "str"), ("f_int", "int"), ("f_bool", "bool"), ("f_float", "float")]


def gen_module(n: int, defer: bool) -> str:
    """Emit a module of N chained recursive Pydantic v2 models."""
    cfg = "    model_config = ConfigDict(defer_build=True)\n" if defer else ""
    out = [
        "from __future__ import annotations",
        "from typing import Optional, List",
        "from pydantic import BaseModel, ConfigDict",
        "",
    ]
    for i in range(n):
        out.append(f"class Model{i}(BaseModel):")
        if cfg:
            out.append(cfg.rstrip("\n"))
        out.append("    id: int")
        for name, typ in SCALARS:
            out.append(f"    {name}: {typ}")
        if i < n - 1:
            out.append(f"    next: Optional[Model{i + 1}] = None")
        if i > 0:
            out.append(f"    prev: Optional[List[Model{i - 1}]] = None")
        out.append("")
    if not defer:
        # eager: resolve all forward refs at import, like the current client does
        for i in range(n):
            out.append(f"Model{i}.model_rebuild()")
    return "\n".join(out) + "\n"


# Driver executed in a fresh subprocess: import the module, optionally raise the
# recursion limit, touch ONE model with scalar-only data, and report.
DRIVER = textwrap.dedent(
    """
    import sys, json, time, resource, importlib
    modpath, n, rlimit = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    if rlimit:
        sys.setrecursionlimit(rlimit)
    sys.path.insert(0, modpath)
    res = {"import_ok": False, "validate_ok": False, "rejects_bad": False, "error": ""}
    try:
        t0 = time.perf_counter()
        mod = importlib.import_module("spikemod")
        res["import_s"] = time.perf_counter() - t0
        res["import_ok"] = True
        # Touch the HEAD of the longest chain with scalar-only data (the case that
        # should stay cheap if cost scales with use, not schema size).
        Model0 = getattr(mod, "Model0")
        t1 = time.perf_counter()
        try:
            inst = Model0(id=1, f_str="x", f_int=2, f_bool=True, f_float=1.5)
        except RecursionError:
            res["error"] = "RecursionError@first-use"
            raise SystemExit
        res["first_use_s"] = time.perf_counter() - t1
        res["validate_ok"] = (inst.id == 1)
        # Confirm validation still rejects bad data.
        try:
            Model0(id="not-an-int", f_str="x", f_int=2, f_bool=True, f_float=1.5)
        except Exception as e:
            res["rejects_bad"] = type(e).__name__ == "ValidationError"
    except SystemExit:
        pass
    except RecursionError:
        res["error"] = "RecursionError@import"
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {e}"[:160]
    res["peak_rss_mb"] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    print(json.dumps(res))
    """
)


def measure(n: int, defer: bool, rlimit: int) -> dict:
    d = Path(tempfile.mkdtemp(prefix="spike_"))
    (d / "spikemod.py").write_text(gen_module(n, defer))
    drv = d / "_drv.py"
    drv.write_text(DRIVER)
    out = subprocess.run(
        [sys.executable, str(drv), str(d), str(n), str(rlimit)],
        capture_output=True, text=True,
    )
    try:
        return json.loads(out.stdout.strip().splitlines()[-1])
    except Exception:
        return {"import_ok": False, "error": (out.stderr or out.stdout).strip()[-160:], "peak_rss_mb": 0}


def main() -> None:
    strategies = [
        ("eager,   default limit", dict(defer=False, rlimit=0)),
        ("eager,   limit=20000", dict(defer=False, rlimit=20000)),
        ("lazy,    default limit", dict(defer=True, rlimit=0)),
        ("lazy,    limit=20000", dict(defer=True, rlimit=20000)),
    ]
    sizes = [20, 50, 100, 200]

    print("\nSPIKE: true-recursive Pydantic v2 models, eager vs lazy (defer_build)")
    print("Touching only Model0 with scalar data. RSS = peak process RSS.\n")
    print(f"pydantic {__import__('pydantic').VERSION}, python {sys.version.split()[0]}, "
          f"default recursionlimit={sys.getrecursionlimit()}\n")

    for label, opts in strategies:
        print(f"== {label} ==")
        print(f"   {'models':>6} {'import':>9} {'first-use':>10} {'RSS(MB)':>9}  result")
        for n in sizes:
            r = measure(n, **opts)
            success = r.get("import_ok") and not r.get("error") and r.get("validate_ok") and r.get("rejects_bad")
            if success:
                imp = f"{r.get('import_s', 0) * 1000:>7.0f}ms"
                fu = f"{r.get('first_use_s', 0) * 1000:>8.1f}ms"
                print(f"   {n:>6} {imp:>9} {fu:>10} {r.get('peak_rss_mb', 0):>9}  valid+rejects-bad")
            else:
                detail = r.get("error") or ("VALIDATION-LOGIC" if r.get("import_ok") else "import failed")
                print(f"   {n:>6} {'--':>9} {'--':>10} {r.get('peak_rss_mb', 0):>9}  FAILED: {detail}")
        print()


if __name__ == "__main__":
    main()
