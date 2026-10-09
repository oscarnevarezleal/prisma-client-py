#!/usr/bin/env python3
"""
Real-world test of memory leak with actual Prisma Client Python.

This test:
1. Uses a real Prisma schema
2. Generates the client
3. Inserts test data
4. Measures memory with both hydration methods
"""

import gc
import sys
import time
import asyncio
import tracemalloc
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / 'src'))

print("🔧 Checking Prisma Client Python installation...")

try:
    from prisma import Prisma
    print("✅ Prisma Client Python found")
except ImportError:
    print("❌ Prisma Client Python not installed")
    print("   Run: pip install -e .")
    sys.exit(1)


# ============================================================================
# Test Schema
# ============================================================================

SCHEMA = """
datasource db {
  provider = "sqlite"
  url      = "file:./leak_test.db"
}

generator client {
  provider = "prisma-client-py"
}

model TestUser {
  id        Int      @id @default(autoincrement())
  name      String
  email     String   @unique
  data      String   // JSON data as string
  createdAt DateTime @default(now())
}
"""


# ============================================================================
# Test Functions
# ============================================================================

async def setup_test_db():
    """Create test database and insert data."""
    print("\n📦 Setting up test database...")
    
    # Write schema
    schema_path = Path("leak_test_schema.prisma")
    schema_path.write_text(SCHEMA)
    
    # Generate client
    print("  Generating Prisma client...")
    import subprocess
    result = subprocess.run(
        ["python", "-m", "prisma", "generate", "--schema", str(schema_path)],
        capture_output=True,
        text=True
    )
    if result.returncode != 0:
        print(f"❌ Generate failed: {result.stderr}")
        return False
    
    # Push schema to database
    print("  Pushing schema to database...")
    result = subprocess.run(
        ["python", "-m", "prisma", "db", "push", "--schema", str(schema_path), "--skip-generate"],
        capture_output=True,
        text=True
    )
    if result.returncode != 0:
        print(f"❌ DB push failed: {result.stderr}")
        return False
    
    # Connect and insert test data
    print("  Inserting test data...")
    db = Prisma()
    await db.connect()
    
    # Clear existing data
    await db.testuser.delete_many()
    
    # Insert 1000 test users
    for i in range(1000):
        await db.testuser.create(
            data={
                'name': f'User_{i}',
                'email': f'user{i}@example.com',
                'data': f'{{"key": "value_{i}", "nested": {{"data": {i}}}}}'
            }
        )
    
    await db.disconnect()
    print("✅ Test database ready with 1,000 users")
    return True


async def test_prisma_with_leak(iterations: int):
    """
    Test Prisma's current behavior (assuming it uses direct ORM objects).
    This may or may not leak depending on current implementation.
    """
    print("\n" + "="*70)
    print("🔴 TEST 1: Current Prisma Hydration")
    print("="*70)
    
    db = Prisma()
    await db.connect()
    
    gc.collect()
    tracemalloc.start()
    start_time = time.perf_counter()
    start_memory = tracemalloc.get_traced_memory()[0]
    
    for iteration in range(iterations):
        # Fetch all users (current behavior)
        users = await db.testuser.find_many()
        
        # Force GC
        gc.collect()
        
        if (iteration + 1) % 10 == 0:
            current, peak = tracemalloc.get_traced_memory()
            print(f"  Iteration {iteration + 1}/{iterations}: "
                  f"Current: {current / 1024 / 1024:.2f} MB, "
                  f"Peak: {peak / 1024 / 1024:.2f} MB")
    
    end_time = time.perf_counter()
    final_current, final_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    
    await db.disconnect()
    
    memory_increase = (final_current - start_memory) / 1024 / 1024
    
    print(f"\n📊 Results:")
    print(f"  Time taken: {end_time - start_time:.2f}s")
    print(f"  Memory increase: {memory_increase:.2f} MB")
    print(f"  Peak memory: {final_peak / 1024 / 1024:.2f} MB")
    
    return memory_increase, end_time - start_time


async def test_prisma_raw_query(iterations: int):
    """
    Test using raw queries (returns dicts, not ORM objects).
    This should not leak.
    """
    print("\n" + "="*70)
    print("🟢 TEST 2: Raw Query (returns dicts)")
    print("="*70)
    
    db = Prisma()
    await db.connect()
    
    gc.collect()
    tracemalloc.start()
    start_time = time.perf_counter()
    start_memory = tracemalloc.get_traced_memory()[0]
    
    for iteration in range(iterations):
        # Use raw query - returns dicts, not Pydantic models
        users = await db.query_raw('SELECT * FROM TestUser')
        
        # Force GC
        gc.collect()
        
        if (iteration + 1) % 10 == 0:
            current, peak = tracemalloc.get_traced_memory()
            print(f"  Iteration {iteration + 1}/{iterations}: "
                  f"Current: {current / 1024 / 1024:.2f} MB, "
                  f"Peak: {peak / 1024 / 1024:.2f} MB")
    
    end_time = time.perf_counter()
    final_current, final_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    
    await db.disconnect()
    
    memory_increase = (final_current - start_memory) / 1024 / 1024
    
    print(f"\n📊 Results:")
    print(f"  Time taken: {end_time - start_time:.2f}s")
    print(f"  Memory increase: {memory_increase:.2f} MB")
    print(f"  Peak memory: {final_peak / 1024 / 1024:.2f} MB")
    
    return memory_increase, end_time - start_time


async def main():
    print("="*70)
    print("🧪 Prisma Client Python - Real-World Memory Leak Test")
    print("="*70)
    
    # Setup
    if not await setup_test_db():
        print("❌ Setup failed")
        return
    
    # Configuration
    ITERATIONS = 50
    
    print(f"\n⚙️  Configuration:")
    print(f"  Iterations: {ITERATIONS}")
    print(f"  Objects per iteration: 1,000")
    print(f"  Total fetches: {ITERATIONS * 1000:,}")
    
    try:
        # Test 1: Current behavior
        leak_memory, leak_time = await test_prisma_with_leak(ITERATIONS)
        
        # Pause for GC
        await asyncio.sleep(1)
        gc.collect()
        
        # Test 2: Raw query (baseline)
        no_leak_memory, no_leak_time = await test_prisma_raw_query(ITERATIONS)
        
        # Analysis
        print("\n" + "="*70)
        print("📈 FINAL ANALYSIS")
        print("="*70)
        
        memory_diff = leak_memory - no_leak_memory
        time_diff = leak_time - no_leak_time
        
        print(f"\n💾 Memory Impact:")
        print(f"  Current behavior: {leak_memory:.2f} MB increase")
        print(f"  Raw query:        {no_leak_memory:.2f} MB increase")
        print(f"  Difference:       {memory_diff:.2f} MB")
        
        if memory_diff > 10:  # More than 10MB difference suggests a leak
            print(f"  ⚠️  Possible memory leak detected!")
        else:
            print(f"  ✅ Memory usage looks normal")
        
        print(f"\n⏱️  Performance Impact:")
        print(f"  Current behavior: {leak_time:.2f}s")
        print(f"  Raw query:        {no_leak_time:.2f}s")
        print(f"  Difference:       {time_diff:.2f}s")
        
    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        import traceback
        traceback.print_exc()
    finally:
        # Cleanup
        print("\n🧹 Cleaning up...")
        Path("leak_test_schema.prisma").unlink(missing_ok=True)
        Path("leak_test.db").unlink(missing_ok=True)
        Path("leak_test.db-journal").unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(main())
