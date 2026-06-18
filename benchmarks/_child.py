"""Cold-import measurement, run as a fresh subprocess.

Usage:
    python _child.py <prisma_parent_dir> <comma_separated_modules>

Prints one JSON line: {"import_time": <s>, "peak_rss_kb": <int>}.
With an empty module list it measures the bare-interpreter baseline so the
caller can subtract interpreter/stdlib overhead.
"""
import sys
import json
import time
import resource
import importlib


def main() -> None:
    parent = sys.argv[1]
    mods = [m for m in sys.argv[2].split(",") if m]

    if parent:
        sys.path.insert(0, parent)

    start = time.perf_counter()
    for mod in mods:
        importlib.import_module(mod)
    elapsed = time.perf_counter() - start

    # ru_maxrss is peak resident set size; KiB on Linux, bytes on macOS.
    peak_rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        peak_rss_kb //= 1024

    print(json.dumps({"import_time": elapsed, "peak_rss_kb": peak_rss_kb}))


if __name__ == "__main__":
    main()
