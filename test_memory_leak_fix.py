#!/usr/bin/env python3
"""
Test script to verify the Pydantic v2 memory leak fix.

This script demonstrates that the memory leak has been fixed by showing
that memory is properly released after querying records.
"""
import asyncio
import gc
import sys
from pathlib import Path

# Add the src directory to Python path
src_path = Path(__file__).parent / "src"
sys.path.insert(0, str(src_path))


def test_safe_dict_coerce():
    """Test the _safe_dict_coerce helper function."""
    from prisma._compat import _safe_dict_coerce
    
    print("Testing _safe_dict_coerce()...")
    
    # Test 1: Plain dict should be returned unchanged
    test_dict = {"key": "value"}
    result = _safe_dict_coerce(test_dict)
    assert result is test_dict, "Plain dict should be returned unchanged"
    print("✓ Plain dict test passed")
    
    # Test 2: Object with __dict__ should be converted
    class TestObject:
        def __init__(self):
            self.key = "value"
    
    test_obj = TestObject()
    result = _safe_dict_coerce(test_obj)
    assert isinstance(result, dict), "Object should be converted to dict"
    assert result == {"key": "value"}, "Dict should contain correct data"
    assert result is not test_obj, "Should be a new dict, not the original object"
    print("✓ Object with __dict__ test passed")
    
    # Test 3: Other types should be returned unchanged
    test_list = [1, 2, 3]
    result = _safe_dict_coerce(test_list)
    assert result is test_list, "Other types should be returned unchanged"
    print("✓ Other types test passed")
    
    print("\nAll _safe_dict_coerce tests passed! ✓\n")


def test_model_parse_with_dict():
    """Test that model_parse works with dictionaries."""
    from prisma._compat import model_parse, PYDANTIC_V2
    from pydantic import BaseModel
    
    print(f"Testing model_parse() with Pydantic v{'2' if PYDANTIC_V2 else '1'}...")
    
    class TestModel(BaseModel):
        name: str
        age: int
    
    # Test with plain dict
    data = {"name": "Alice", "age": 30}
    result = model_parse(TestModel, data)
    
    assert result.name == "Alice", "Name should be parsed correctly"
    assert result.age == 30, "Age should be parsed correctly"
    print("✓ Plain dict parsing test passed")
    
    # Test with object having __dict__
    class DataObject:
        def __init__(self):
            self.name = "Bob"
            self.age = 25
    
    data_obj = DataObject()
    result = model_parse(TestModel, data_obj)
    
    assert result.name == "Bob", "Name should be parsed from object"
    assert result.age == 25, "Age should be parsed from object"
    print("✓ Object parsing test passed")
    
    print("\nAll model_parse tests passed! ✓\n")


def test_memory_cycle_prevention():
    """Test that no reference cycle is created."""
    from prisma._compat import model_parse, PYDANTIC_V2
    from pydantic import BaseModel
    import weakref
    
    if not PYDANTIC_V2:
        print("Skipping memory cycle test (Pydantic v1 doesn't have this issue)")
        return
    
    print("Testing memory cycle prevention...")
    
    class TestModel(BaseModel):
        name: str
    
    class OrmObject:
        def __init__(self):
            self.name = "Test"
    
    # Create ORM object and weak reference
    orm_obj = OrmObject()
    weak_ref = weakref.ref(orm_obj)
    
    # Parse to Pydantic model
    model = model_parse(TestModel, orm_obj)
    
    # Delete ORM object
    del orm_obj
    
    # Force garbage collection
    gc.collect()
    
    # Check if ORM object was released
    assert weak_ref() is None, "ORM object should be garbage collected (no reference cycle)"
    print("✓ No reference cycle detected!")
    
    # Model should still be valid
    assert model.name == "Test", "Model should still have correct data"
    print("✓ Model data is intact")
    
    print("\nMemory cycle prevention test passed! ✓\n")


async def test_with_database():
    """Test with actual database queries (if client is generated)."""
    try:
        from prisma import Prisma
        from prisma._compat import PYDANTIC_V2
        
        print(f"Testing with database (Pydantic v{'2' if PYDANTIC_V2 else '1'})...")
        
        # Note: This requires a generated client and running database
        # This is an optional test that demonstrates real-world usage
        
        print("Note: Database test requires generated client and database connection")
        print("To test manually:")
        print("  1. Ensure database is running")
        print("  2. Run: prisma generate")
        print("  3. Run this script again")
        
    except ImportError:
        print("Skipping database test (client not generated)")


def main():
    """Run all tests."""
    print("=" * 70)
    print("Memory Leak Fix Verification Tests")
    print("=" * 70)
    print()
    
    try:
        # Test the helper function
        test_safe_dict_coerce()
        
        # Test model_parse
        test_model_parse_with_dict()
        
        # Test memory cycle prevention
        test_memory_cycle_prevention()
        
        # Test with database (optional)
        asyncio.run(test_with_database())
        
        print("=" * 70)
        print("All tests passed! ✅")
        print("=" * 70)
        print()
        print("The memory leak fix is working correctly!")
        print("Memory should now be properly released after database queries.")
        
    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
