#!/usr/bin/env python3
"""
Simple memory test using only tracemalloc (built-in).
"""

import gc
import tracemalloc


def format_bytes(size: int) -> str:
    """Format bytes to human readable format."""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024.0:
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} TB"


print("=" * 70)
print("Memory Usage Test - Prisma Client Python (200 models)")
print("=" * 70)

tracemalloc.start()
gc.collect()

baseline = tracemalloc.take_snapshot()
print(f"\n✓ Baseline snapshot taken")

# Import prisma
print("🔄 Importing prisma...")
from src import prisma
gc.collect()

after_prisma = tracemalloc.take_snapshot()
stats = after_prisma.compare_to(baseline, 'lineno')
allocated = sum(stat.size_diff for stat in stats if stat.size_diff > 0)
print(f"✓ After prisma: {format_bytes(allocated)} allocated")

# Import Prisma client
print("🔄 Importing Prisma client...")
from src.prisma import Prisma
gc.collect()

after_client = tracemalloc.take_snapshot()
stats = after_client.compare_to(after_prisma, 'lineno')
allocated = sum(stat.size_diff for stat in stats if stat.size_diff > 0)
print(f"✓ After Prisma client: {format_bytes(allocated)} allocated")

# Import models
print("🔄 Importing models...")
from src.prisma import models
gc.collect()

after_models = tracemalloc.take_snapshot()
stats = after_models.compare_to(after_client, 'lineno')
allocated = sum(stat.size_diff for stat in stats if stat.size_diff > 0)
model_count = len([attr for attr in dir(models) if attr.startswith('Model')])
print(f"✓ After models ({model_count} models): {format_bytes(allocated)} allocated")
print(f"  Average per model: {format_bytes(allocated // model_count)}")

# Import types
print("🔄 Importing types...")
from src.prisma import types
gc.collect()

after_types = tracemalloc.take_snapshot()
stats = after_types.compare_to(after_models, 'lineno')
allocated = sum(stat.size_diff for stat in stats if stat.size_diff > 0)
print(f"✓ After types: {format_bytes(allocated)} allocated")

# Total
print("\n" + "=" * 70)
stats = after_types.compare_to(baseline, 'lineno')
total = sum(stat.size_diff for stat in stats if stat.size_diff > 0)
print(f"📊 TOTAL ALLOCATED: {format_bytes(total)}")
print("=" * 70)

current, peak = tracemalloc.get_traced_memory()
print(f"\n📈 Current: {format_bytes(current)}")
print(f"📈 Peak: {format_bytes(peak)}")

tracemalloc.stop()
