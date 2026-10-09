#!/usr/bin/env python3
"""
Test script to verify scalar_fields_only configuration works correctly.

This script:
1. Generates the Prisma client with scalar_fields_only=true
2. Verifies that relationship fields are excluded from models
3. Verifies that scalar fields are present
4. Shows the difference in model structure
"""

import subprocess
import sys
from pathlib import Path

def run_command(cmd: list[str], description: str) -> None:
    """Run a command and display output."""
    print(f"\n{'='*60}")
    print(f"{description}")
    print(f"{'='*60}")
    print(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    if result.returncode != 0:
        print(f"❌ Error: {result.stderr}")
        sys.exit(1)
    
    if result.stdout:
        print(result.stdout)
    
    print(f"✅ {description} completed")

def check_model_fields(test_dir: Path) -> None:
    """Check the generated model fields."""
    print(f"\n{'='*60}")
    print("Checking Generated Model Fields")
    print(f"{'='*60}")
    
    # Check User model
    user_model = test_dir / "models" / "_user.py"
    if not user_model.exists():
        # Try non-separate model files
        user_model = test_dir / "models.py"
    
    if user_model.exists():
        content = user_model.read_text()
        
        # Expected scalar fields
        scalar_fields = ['id:', 'email:', 'name:', 'createdAt:']
        # Relationship fields that should NOT be present
        relation_fields = ['posts:', 'profile:']
        
        print("\n🔍 Checking User model...")
        for field in scalar_fields:
            if field in content:
                print(f"  ✅ Found scalar field: {field}")
            else:
                print(f"  ❌ Missing scalar field: {field}")
        
        for field in relation_fields:
            if field not in content:
                print(f"  ✅ Relationship field correctly excluded: {field}")
            else:
                print(f"  ❌ Relationship field should be excluded: {field}")
    else:
        print("❌ Could not find generated model file")

def main():
    """Main test function."""
    project_root = Path(__file__).parent
    schema_file = project_root / "test_scalar_only.prisma"
    test_dir = project_root / "test_generated"
    
    if not schema_file.exists():
        print(f"❌ Schema file not found: {schema_file}")
        sys.exit(1)
    
    # Clean previous test output
    if test_dir.exists():
        import shutil
        shutil.rmtree(test_dir)
        print(f"🧹 Cleaned previous test output: {test_dir}")
    
    # Generate the client
    run_command(
        ["python", "-m", "prisma", "generate", "--schema", str(schema_file)],
        "Generating Prisma Client with scalar_fields_only=true"
    )
    
    # Check the generated models
    if test_dir.exists():
        check_model_fields(test_dir)
        
        print(f"\n{'='*60}")
        print("✅ Test Complete!")
        print(f"{'='*60}")
        print(f"\nGenerated files are in: {test_dir}")
        print("\nYou can inspect the generated models to verify:")
        print(f"  - {test_dir}/models.py (or models/_*.py for separate files)")
        print(f"  - {test_dir}/types.py")
    else:
        print("❌ Generated directory not found")
        sys.exit(1)

if __name__ == "__main__":
    main()
