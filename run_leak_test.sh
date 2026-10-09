#!/bin/bash
# Run the memory leak test

echo "🧪 Running Pydantic Memory Leak Test"
echo "======================================"
echo ""

# Check if pydantic is installed
python3 -c "import pydantic" 2>/dev/null
if [ $? -ne 0 ]; then
    echo "❌ Pydantic not found. Installing..."
    pip install pydantic
fi

# Run the test
python3 test_memory_leak.py

echo ""
echo "======================================"
echo "✅ Test complete!"
