#!/bin/bash
# Run all memory leak tests and summarize results

echo "╔════════════════════════════════════════════════════════════════════╗"
echo "║         Prisma Client Python - Memory Leak Test Suite             ║"
echo "╚════════════════════════════════════════════════════════════════════╝"
echo ""

# Colors for output
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Check Python
echo "📋 Checking Python installation..."
python3 --version
echo ""

# Check dependencies
echo "📦 Checking dependencies..."
python3 -c "import pydantic; print(f'✓ Pydantic {pydantic.VERSION}')" 2>/dev/null || {
    echo "❌ Pydantic not found. Installing..."
    pip install -q pydantic
}

python3 -c "import tracemalloc; print('✓ tracemalloc available')" 2>/dev/null || {
    echo "❌ tracemalloc not available"
    exit 1
}
echo ""

# Test 1: Mock ORM leak test
echo "╔════════════════════════════════════════════════════════════════════╗"
echo "║  TEST 1: Mock ORM Memory Leak (Confirms __dict__ fix works)       ║"
echo "╚════════════════════════════════════════════════════════════════════╝"
echo ""
echo "This test simulates the known Pydantic memory leak with ORM objects"
echo "and confirms that using __dict__ fixes it."
echo ""
echo -e "${YELLOW}Starting test...${NC}"
echo ""

python3 test_memory_leak.py

TEST1_EXIT=$?

echo ""
echo "═══════════════════════════════════════════════════════════════════════"
echo ""

if [ $TEST1_EXIT -eq 0 ]; then
    echo -e "${GREEN}✅ Test 1 completed successfully${NC}"
else
    echo -e "${RED}❌ Test 1 failed with exit code $TEST1_EXIT${NC}"
fi

echo ""
echo "Press Enter to continue to Test 2..."
read

# Test 2: Real Prisma test (if Prisma is installed)
echo ""
echo "╔════════════════════════════════════════════════════════════════════╗"
echo "║  TEST 2: Real Prisma Client Memory Test (Checks actual behavior)  ║"
echo "╚════════════════════════════════════════════════════════════════════╝"
echo ""
echo "This test uses actual Prisma Client Python to check if a leak"
echo "exists in real-world usage."
echo ""

# Check if Prisma is installed
python3 -c "import prisma" 2>/dev/null
if [ $? -eq 0 ]; then
    echo -e "${YELLOW}Starting test...${NC}"
    echo ""
    python3 test_prisma_leak.py
    TEST2_EXIT=$?
    echo ""
    echo "═══════════════════════════════════════════════════════════════════"
    echo ""
    if [ $TEST2_EXIT -eq 0 ]; then
        echo -e "${GREEN}✅ Test 2 completed successfully${NC}"
    else
        echo -e "${RED}❌ Test 2 failed with exit code $TEST2_EXIT${NC}"
    fi
else
    echo -e "${YELLOW}⚠️  Prisma Client Python not installed, skipping Test 2${NC}"
    echo ""
    echo "To run this test:"
    echo "  1. Install the package: pip install -e ."
    echo "  2. Run: python test_prisma_leak.py"
    TEST2_EXIT=-1
fi

# Summary
echo ""
echo "╔════════════════════════════════════════════════════════════════════╗"
echo "║                          SUMMARY                                   ║"
echo "╚════════════════════════════════════════════════════════════════════╝"
echo ""

if [ $TEST1_EXIT -eq 0 ]; then
    echo -e "${GREEN}✅ Test 1 (Mock ORM):${NC} Passed"
    echo "   → Confirmed: __dict__ workaround prevents memory leak"
else
    echo -e "${RED}❌ Test 1 (Mock ORM):${NC} Failed"
fi

if [ $TEST2_EXIT -eq 0 ]; then
    echo -e "${GREEN}✅ Test 2 (Real Prisma):${NC} Passed"
    echo "   → Check output above for memory analysis"
elif [ $TEST2_EXIT -eq -1 ]; then
    echo -e "${YELLOW}⚠️  Test 2 (Real Prisma):${NC} Skipped (not installed)"
else
    echo -e "${RED}❌ Test 2 (Real Prisma):${NC} Failed"
fi

echo ""
echo "📄 For detailed findings, see: MEMORY_LEAK_FIX_LOCATION.md"
echo ""

if [ $TEST1_EXIT -eq 0 ] && [ $TEST2_EXIT -eq 0 ]; then
    echo -e "${GREEN}🎉 All tests passed!${NC}"
    exit 0
elif [ $TEST1_EXIT -eq 0 ] && [ $TEST2_EXIT -eq -1 ]; then
    echo -e "${YELLOW}⚠️  Test 1 passed, Test 2 skipped${NC}"
    exit 0
else
    echo -e "${RED}⚠️  Some tests failed${NC}"
    exit 1
fi
