#!/usr/bin/env python3
"""
Test to confirm the Pydantic model_validate memory leak with ORM objects.

This test demonstrates:
1. Memory leak when using model_validate(orm_object)
2. No leak when using model_validate(orm_object.__dict__)
3. Performance comparison between both methods

Based on research: https://github.com/pydantic/pydantic/issues/9429
"""

import gc
import sys
import time
import tracemalloc
from typing import Optional, List
from pathlib import Path

# Add src to path so we can import prisma
sys.path.insert(0, str(Path(__file__).parent / 'src'))

try:
    from pydantic import BaseModel, field_validator
except ImportError:
    print("❌ Pydantic not installed. Run: pip install pydantic")
    sys.exit(1)


# ============================================================================
# Simulated ORM Object (mimics Prisma's internal objects)
# ============================================================================

class MockORMObject:
    """Simulates an ORM object with an identity map reference."""
    
    _instances = []  # Simulates ORM session's identity map
    
    def __init__(self, id: int, name: str, email: str, data: dict):
        self.id = id
        self.name = name
        self.email = email
        self.data = data
        # Simulate ORM tracking
        MockORMObject._instances.append(self)
    
    def __dict__(self):
        """Return a clean dict without ORM tracking."""
        return {
            'id': self.id,
            'name': self.name,
            'email': self.email,
            'data': self.data
        }
    
    @classmethod
    def reset_tracking(cls):
        """Clear the identity map."""
        cls._instances.clear()


# ============================================================================
# Pydantic Model (mimics generated Prisma models)
# ============================================================================

class User(BaseModel):
    """Simulates a generated Prisma Pydantic model."""
    id: int
    name: str
    email: str
    data: dict
    
    @field_validator('name')
    @classmethod
    def validate_name(cls, v: str) -> str:
        if len(v) < 2:
            raise ValueError('Name too short')
        return v
    
    @field_validator('email')
    @classmethod
    def validate_email(cls, v: str) -> str:
        if '@' not in v:
            raise ValueError('Invalid email')
        return v
    
    class Config:
        from_attributes = True  # This enables ORM mode (the leak trigger)


# ============================================================================
# Test Functions
# ============================================================================

def create_mock_orm_objects(count: int) -> List[MockORMObject]:
    """Create mock ORM objects."""
    objects = []
    for i in range(count):
        obj = MockORMObject(
            id=i,
            name=f"User_{i}",
            email=f"user{i}@example.com",
            data={'key': f'value_{i}', 'nested': {'data': i}}
        )
        objects.append(obj)
    return objects


def test_with_orm_object_direct(orm_objects: List[MockORMObject], iterations: int) -> tuple:
    """
    Test using model_validate(orm_object) directly.
    This SHOULD cause a memory leak.
    """
    print("\n" + "="*70)
    print("🔴 TEST 1: Using model_validate(orm_object) - SHOULD LEAK")
    print("="*70)
    
    gc.collect()
    tracemalloc.start()
    start_time = time.perf_counter()
    start_memory = tracemalloc.get_traced_memory()[0]
    
    validated_models = []
    
    for iteration in range(iterations):
        for orm_obj in orm_objects:
            # This is the LEAKY pattern
            model = User.model_validate(orm_obj)
            validated_models.append(model)
        
        # Force garbage collection
        gc.collect()
        
        # Report every 100 iterations
        if (iteration + 1) % 100 == 0:
            current, peak = tracemalloc.get_traced_memory()
            print(f"  Iteration {iteration + 1}/{iterations}: "
                  f"Current: {current / 1024 / 1024:.2f} MB, "
                  f"Peak: {peak / 1024 / 1024:.2f} MB")
    
    end_time = time.perf_counter()
    final_current, final_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    
    # Calculate memory increase
    memory_increase = (final_current - start_memory) / 1024 / 1024
    
    print(f"\n📊 Results:")
    print(f"  Total models created: {len(validated_models):,}")
    print(f"  Time taken: {end_time - start_time:.2f}s")
    print(f"  Memory increase: {memory_increase:.2f} MB")
    print(f"  Peak memory: {final_peak / 1024 / 1024:.2f} MB")
    print(f"  ORM instances tracked: {len(MockORMObject._instances)}")
    
    # Clear for next test
    validated_models.clear()
    MockORMObject.reset_tracking()
    gc.collect()
    
    return memory_increase, end_time - start_time


def test_with_dict_workaround(orm_objects: List[MockORMObject], iterations: int) -> tuple:
    """
    Test using model_validate(orm_object.__dict__).
    This SHOULD NOT leak - the reference cycle is broken.
    """
    print("\n" + "="*70)
    print("🟢 TEST 2: Using model_validate(orm_object.__dict__) - NO LEAK")
    print("="*70)
    
    gc.collect()
    tracemalloc.start()
    start_time = time.perf_counter()
    start_memory = tracemalloc.get_traced_memory()[0]
    
    validated_models = []
    
    for iteration in range(iterations):
        for orm_obj in orm_objects:
            # This is the FIXED pattern - use __dict__
            model = User.model_validate(orm_obj.__dict__())
            validated_models.append(model)
        
        # Force garbage collection
        gc.collect()
        
        # Report every 100 iterations
        if (iteration + 1) % 100 == 0:
            current, peak = tracemalloc.get_traced_memory()
            print(f"  Iteration {iteration + 1}/{iterations}: "
                  f"Current: {current / 1024 / 1024:.2f} MB, "
                  f"Peak: {peak / 1024 / 1024:.2f} MB")
    
    end_time = time.perf_counter()
    final_current, final_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    
    # Calculate memory increase
    memory_increase = (final_current - start_memory) / 1024 / 1024
    
    print(f"\n📊 Results:")
    print(f"  Total models created: {len(validated_models):,}")
    print(f"  Time taken: {end_time - start_time:.2f}s")
    print(f"  Memory increase: {memory_increase:.2f} MB")
    print(f"  Peak memory: {final_peak / 1024 / 1024:.2f} MB")
    print(f"  ORM instances tracked: {len(MockORMObject._instances)}")
    
    # Clear for next test
    validated_models.clear()
    MockORMObject.reset_tracking()
    gc.collect()
    
    return memory_increase, end_time - start_time


def test_performance_only(orm_objects: List[MockORMObject], iterations: int):
    """
    Quick performance test without memory tracking.
    """
    print("\n" + "="*70)
    print("⚡ PERFORMANCE COMPARISON")
    print("="*70)
    
    # Test 1: Direct ORM object
    gc.collect()
    start = time.perf_counter()
    for _ in range(iterations):
        for orm_obj in orm_objects:
            model = User.model_validate(orm_obj)
    time_direct = time.perf_counter() - start
    
    gc.collect()
    
    # Test 2: Using __dict__
    start = time.perf_counter()
    for _ in range(iterations):
        for orm_obj in orm_objects:
            model = User.model_validate(orm_obj.__dict__())
    time_dict = time.perf_counter() - start
    
    print(f"  Direct ORM: {time_direct:.3f}s")
    print(f"  Using __dict__: {time_dict:.3f}s")
    
    if time_dict < time_direct:
        speedup = (time_direct - time_dict) / time_direct * 100
        print(f"  ✅ __dict__ is {speedup:.1f}% FASTER")
    else:
        slowdown = (time_dict - time_direct) / time_direct * 100
        print(f"  ⚠️  __dict__ is {slowdown:.1f}% slower")


# ============================================================================
# Main Test Runner
# ============================================================================

def main():
    print("="*70)
    print("🧪 Pydantic Memory Leak Test - ORM Object Hydration")
    print("="*70)
    print("\nThis test simulates Prisma Client Python's model hydration.")
    print("Based on: https://github.com/pydantic/pydantic/issues/9429")
    
    # Configuration
    NUM_OBJECTS = 100  # Number of ORM objects per iteration
    ITERATIONS = 500   # Number of times to process all objects
    
    print(f"\n⚙️  Configuration:")
    print(f"  Objects per iteration: {NUM_OBJECTS}")
    print(f"  Iterations: {ITERATIONS}")
    print(f"  Total validations: {NUM_OBJECTS * ITERATIONS:,}")
    
    # Create mock ORM objects
    print(f"\n📦 Creating {NUM_OBJECTS} mock ORM objects...")
    orm_objects = create_mock_orm_objects(NUM_OBJECTS)
    
    # Run tests
    try:
        # Test 1: Direct ORM object (should leak)
        leak_memory, leak_time = test_with_orm_object_direct(orm_objects, ITERATIONS)
        
        # Small pause to ensure GC runs
        time.sleep(1)
        gc.collect()
        
        # Test 2: Using __dict__ (should not leak)
        no_leak_memory, no_leak_time = test_with_dict_workaround(orm_objects, ITERATIONS)
        
        # Performance comparison
        test_performance_only(orm_objects, ITERATIONS)
        
        # Final Analysis
        print("\n" + "="*70)
        print("📈 FINAL ANALYSIS")
        print("="*70)
        
        memory_saved = leak_memory - no_leak_memory
        time_diff = leak_time - no_leak_time
        
        print(f"\n💾 Memory Impact:")
        print(f"  Direct ORM:     {leak_memory:.2f} MB increase")
        print(f"  Using __dict__: {no_leak_memory:.2f} MB increase")
        print(f"  Memory saved:   {memory_saved:.2f} MB ({memory_saved / leak_memory * 100:.1f}% reduction)")
        
        print(f"\n⏱️  Performance Impact:")
        print(f"  Direct ORM:     {leak_time:.2f}s")
        print(f"  Using __dict__: {no_leak_time:.2f}s")
        if time_diff > 0:
            print(f"  Time saved:     {time_diff:.2f}s ({time_diff / leak_time * 100:.1f}% faster)")
        else:
            print(f"  Time overhead:  {abs(time_diff):.2f}s ({abs(time_diff) / leak_time * 100:.1f}% slower)")
        
        # Conclusion
        print("\n" + "="*70)
        print("🎯 CONCLUSION")
        print("="*70)
        
        if memory_saved > leak_memory * 0.5:  # If we save more than 50%
            print("✅ CONFIRMED: Using __dict__ significantly reduces memory usage!")
            print("✅ This validates the fix for the memory leak.")
            
            if time_diff >= 0:
                print("✅ BONUS: The fix is also faster or equal in performance!")
            else:
                print("⚠️  NOTE: Small performance overhead, but worth it for memory savings.")
        else:
            print("⚠️  WARNING: Memory savings not as significant as expected.")
            print("   More investigation needed.")
        
        print("\n💡 Recommendation:")
        print("   Modify Prisma Client Python's hydration logic to use:")
        print("   model_validate(orm_object.__dict__) instead of model_validate(orm_object)")
        
    except KeyboardInterrupt:
        print("\n\n⚠️  Test interrupted by user.")
    except Exception as e:
        print(f"\n\n❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
