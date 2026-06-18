# Memory Leak Tests - Quick Start

## Overview

This directory contains tests to confirm and fix the Pydantic memory leak issue documented in:
- Research: https://github.com/pydantic/pydantic/issues/9429
- Analysis: `Python ORM Memory Optimization Strategies.md`

## Quick Start

### Run All Tests

```bash
chmod +x run_all_leak_tests.sh
./run_all_leak_tests.sh
```

This will run both tests and provide a summary.

---

## Test Descriptions

### Test 1: Mock ORM Memory Leak (`test_memory_leak.py`)

**Purpose**: Confirm the known Pydantic memory leak exists and that `__dict__` fixes it.

**What it does**:
1. Creates mock ORM objects (simulates Prisma engine objects)
2. Hydrates them using `model_validate(orm_object)` - **should leak**
3. Hydrates them using `model_validate(orm_object.__dict__)` - **should not leak**
4. Compares memory usage

**Expected result**:
```
🔴 TEST 1: Direct ORM - Memory increase: ~50-100 MB (LEAK)
🟢 TEST 2: Using __dict__ - Memory increase: ~2-5 MB (NO LEAK)
✅ Memory saved: ~90-95% reduction
```

**Run individually**:
```bash
python test_memory_leak.py
```

---

### Test 2: Real Prisma Memory Test (`test_prisma_leak.py`)

**Purpose**: Test actual Prisma Client Python to see if the leak occurs in practice.

**What it does**:
1. Creates a real SQLite database with test data
2. Runs multiple queries using Prisma Client
3. Compares with raw SQL queries (baseline)
4. Measures memory usage over iterations

**Expected result**:
- If **NO leak**: Both tests show similar memory usage (~10-20 MB)
- If **leak exists**: First test shows higher memory usage

**Run individually**:
```bash
python test_prisma_leak.py
```

**Note**: Requires Prisma Client Python to be installed:
```bash
pip install -e .
```

---

## Understanding the Results

### Test 1: Mock ORM Test

| Memory Behavior | Interpretation | Action |
|----------------|----------------|---------|
| **Large difference** (>50MB) | Leak confirmed with direct ORM objects | Implement `__dict__` fix |
| **Small difference** (<5MB) | No significant leak | Investigate further |

### Test 2: Real Prisma Test

| Memory Behavior | Interpretation | Action |
|----------------|----------------|---------|
| **Similar memory** (both ~10-20MB) | ✅ No leak in Prisma | Document & move on |
| **Prisma higher** (>2x difference) | ⚠️ Possible leak | Implement fix |

---

## Interpreting Results

### Scenario A: Test 1 Shows Leak, Test 2 Shows NO Leak

**Conclusion**: The `__dict__` workaround works in principle, but Prisma's engine already returns plain dicts, so **no fix needed**.

**Action**: Document findings in `OPTIMIZATION_SUMMARY.md`

---

### Scenario B: Both Tests Show Leak

**Conclusion**: Memory leak confirmed in real-world Prisma usage.

**Action**: Implement fix in `src/prisma/_compat.py`:

```python
def model_parse(model: type[_ModelT], obj: Any) -> _ModelT:
    if PYDANTIC_V2:
        # Fix: Use __dict__ if object has it (breaks reference cycle)
        if hasattr(obj, '__dict__'):
            obj_dict = obj.__dict__ if callable(obj.__dict__) else obj.__dict__
            return model.model_validate(obj_dict)
        return model.model_validate(obj)
    else:
        return model.parse_obj(obj)
```

**Test the fix**:
```bash
# Apply the fix above, then re-run:
./run_all_leak_tests.sh
```

Expected: Memory usage should drop significantly in Test 2.

---

### Scenario C: Test 1 Shows NO Leak

**Conclusion**: Something is wrong with the test setup.

**Action**: Review test code, check Pydantic version, try with different configurations.

---

## Files in This Directory

| File | Purpose |
|------|---------|
| `test_memory_leak.py` | Mock ORM test (confirms leak pattern) |
| `test_prisma_leak.py` | Real Prisma test (measures actual behavior) |
| `run_all_leak_tests.sh` | Runs both tests with nice formatting |
| `MEMORY_LEAK_FIX_LOCATION.md` | Detailed analysis of where to fix |
| `MEMORY_LEAK_TESTS_README.md` | This file |

---

## Next Steps After Testing

1. **Run the tests**:
   ```bash
   ./run_all_leak_tests.sh
   ```

2. **Analyze results**:
   - Check memory increase in each test
   - Compare against expected values

3. **Take action**:
   - **If leak confirmed**: Implement fix in `_compat.py`
   - **If no leak**: Document why (engine returns dicts)

4. **Document findings**:
   - Update `OPTIMIZATION_SUMMARY.md`
   - Add to `VALIDATION_MEMORY_TRADEOFF.md`

5. **Proceed to next optimization**:
   - GC tuning (20% speedup)
   - `__slots__` implementation
   - Iterator support

---

## Troubleshooting

### Test 1 Won't Run

**Error**: `ImportError: No module named 'pydantic'`

**Fix**:
```bash
pip install pydantic
```

---

### Test 2 Won't Run

**Error**: `ImportError: No module named 'prisma'`

**Fix**:
```bash
cd /Users/oscarnevarez/playground/prisma-client-py
pip install -e .
```

---

### Memory Numbers Look Wrong

**Issue**: Numbers seem too high or too low

**Check**:
1. Close other applications (free up memory)
2. Run multiple times (get average)
3. Increase iteration count for clearer signal

---

### Tests Take Too Long

**Issue**: Tests running for >5 minutes

**Fix**: Edit test files and reduce:
- `NUM_OBJECTS` (default: 100)
- `ITERATIONS` (default: 500)

For quick tests:
```python
NUM_OBJECTS = 50
ITERATIONS = 100
```

---

## Contact / Questions

See main project documentation:
- `VALIDATION_MEMORY_TRADEOFF.md` - Full analysis
- `OPTIMIZATION_SUMMARY.md` - All optimizations
- `Python ORM Memory Optimization Strategies.md` - Research

**Ready to test!** Run `./run_all_leak_tests.sh` to get started.
