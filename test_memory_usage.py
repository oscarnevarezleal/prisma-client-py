#!/usr/bin/env python3
"""
Memory profiling test for Prisma Client Python with many models.
Tests the hypothesis that lazy loading reduces memory usage.
"""

import sys
import gc
import tracemalloc
from typing import List, Tuple


def get_memory_mb() -> float:
    """Get current memory usage in MB."""
    import psutil
    import os
    process = psutil.Process(os.getpid())
    return process.memory_info().rss / 1024 / 1024


def format_bytes(size: int) -> str:
    """Format bytes to human readable format."""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024.0:
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} TB"


def test_import_memory() -> List[Tuple[str, float, int]]:
    """Test memory usage at different import stages."""
    results = []

    # Start memory tracking
    tracemalloc.start()
    gc.collect()

    # Baseline
    baseline_mem = get_memory_mb()
    baseline_trace = tracemalloc.take_snapshot()
    results.append(("Baseline (before any imports)", baseline_mem, 0))
    print(f"📊 Baseline memory: {baseline_mem:.2f} MB")

    # Import prisma package
    print("\n🔄 Importing prisma package...")
    from src import prisma
    gc.collect()

    after_prisma_mem = get_memory_mb()
    after_prisma_trace = tracemalloc.take_snapshot()
    prisma_diff = after_prisma_mem - baseline_mem

    # Get memory diff details
    top_stats = after_prisma_trace.compare_to(baseline_trace, 'lineno')
    prisma_allocated = sum(stat.size_diff for stat in top_stats if stat.size_diff > 0)

    results.append(("After importing prisma", after_prisma_mem, prisma_allocated))
    print(f"📊 After prisma import: {after_prisma_mem:.2f} MB (+{prisma_diff:.2f} MB)")
    print(f"   Allocated: {format_bytes(prisma_allocated)}")

    # Import Prisma client
    print("\n🔄 Importing Prisma client...")
    from src.prisma import Prisma
    gc.collect()

    after_client_mem = get_memory_mb()
    after_client_trace = tracemalloc.take_snapshot()
    client_diff = after_client_mem - after_prisma_mem

    top_stats = after_client_trace.compare_to(after_prisma_trace, 'lineno')
    client_allocated = sum(stat.size_diff for stat in top_stats if stat.size_diff > 0)

    results.append(("After importing Prisma client", after_client_mem, client_allocated))
    print(f"📊 After Prisma client: {after_client_mem:.2f} MB (+{client_diff:.2f} MB)")
    print(f"   Allocated: {format_bytes(client_allocated)}")

    # Import all models
    print("\n🔄 Importing all models...")
    from src.prisma import models
    gc.collect()

    after_models_mem = get_memory_mb()
    after_models_trace = tracemalloc.take_snapshot()
    models_diff = after_models_mem - after_client_mem

    top_stats = after_models_trace.compare_to(after_client_trace, 'lineno')
    models_allocated = sum(stat.size_diff for stat in top_stats if stat.size_diff > 0)

    results.append(("After importing models", after_models_mem, models_allocated))
    print(f"📊 After models import: {after_models_mem:.2f} MB (+{models_diff:.2f} MB)")
    print(f"   Allocated: {format_bytes(models_allocated)}")

    # Count models
    model_count = len([attr for attr in dir(models) if attr.startswith('Model')])
    print(f"   Found {model_count} models")

    # Import types
    print("\n🔄 Importing types...")
    from src.prisma import types
    gc.collect()

    after_types_mem = get_memory_mb()
    after_types_trace = tracemalloc.take_snapshot()
    types_diff = after_types_mem - after_models_mem

    top_stats = after_types_trace.compare_to(after_models_trace, 'lineno')
    types_allocated = sum(stat.size_diff for stat in top_stats if stat.size_diff > 0)

    results.append(("After importing types", after_types_mem, types_allocated))
    print(f"📊 After types import: {after_types_mem:.2f} MB (+{types_diff:.2f} MB)")
    print(f"   Allocated: {format_bytes(types_allocated)}")

    # Get top memory consumers
    print("\n📈 Top 10 memory consumers:")
    snapshot = tracemalloc.take_snapshot()
    top_stats = snapshot.compare_to(baseline_trace, 'lineno')

    for i, stat in enumerate(top_stats[:10], 1):
        if stat.size_diff > 0:
            print(f"   {i}. {stat.traceback.format()[0]}")
            print(f"      Size: {format_bytes(stat.size_diff)}")

    tracemalloc.stop()

    return results


def main():
    print("=" * 70)
    print("Memory Usage Test - Prisma Client Python")
    print("Testing with 200 models")
    print("=" * 70)

    # Check if psutil is available
    try:
        import psutil
    except ImportError:
        print("\n⚠️  psutil not installed. Installing...")
        import subprocess
        subprocess.check_call([sys.executable, "-m", "pip", "install", "psutil", "-q"])
        import psutil

    results = test_import_memory()

    # Summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)

    total_increase = results[-1][1] - results[0][1]
    total_allocated = results[-1][2]

    print(f"\n📊 Total memory increase: {total_increase:.2f} MB")
    print(f"📊 Total allocated: {format_bytes(total_allocated)}")

    print("\n📋 Breakdown:")
    for stage, mem, allocated in results:
        print(f"   {stage:.<45} {mem:>8.2f} MB")

    # Per-model average
    model_memory = results[3][1] - results[2][1]  # models import - client import
    avg_per_model = model_memory / 200
    print(f"\n📊 Average memory per model: {avg_per_model:.4f} MB ({avg_per_model * 1024:.2f} KB)")

    print("\n" + "=" * 70)


if __name__ == "__main__":
    main()
