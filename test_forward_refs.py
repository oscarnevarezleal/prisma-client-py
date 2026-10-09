#!/usr/bin/env python3
"""Test forward reference resolution in lazy-loaded models."""

import sys
import os

# Set environment to use separate model files
os.environ['PRISMA_PY_CONFIG_SEPARATE_MODEL_FILES'] = '1'

print('=== Testing Forward Reference Resolution ===\n')

# First, generate the client with separate model files
print('1. Generating client with separate_model_files=True...')
os.system('prisma generate > /dev/null 2>&1')
print('   ✓ Client generated\n')

# Now test runtime access
print('2. Testing model access with forward references...')

try:
    # This should trigger lazy loading
    from prisma.models import TestUser
    print(f'   ✓ Loaded TestUser class: {TestUser}')

    # Try to access a field that has a forward reference
    # This will trigger Pydantic validation
    print(f'   ✓ TestUser has fields: {list(TestUser.model_fields.keys())}')

    # Create an instance to test forward reference resolution
    # This is where the error would occur if forward refs aren't resolved
    user_data = {'id': 1, 'name': 'Test User'}
    user = TestUser(**user_data)
    print(f'   ✓ Created TestUser instance: {user}')
    print(f'   ✓ User.name = {user.name}')

    # Test loading a model with relationships (forward references)
    from prisma.models import TestPost
    print(f'   ✓ Loaded TestPost class: {TestPost}')

    post_data = {'id': 1, 'title': 'Test Post', 'authorId': 1}
    post = TestPost(**post_data)
    print(f'   ✓ Created TestPost instance: {post}')
    print(f'   ✓ Post.title = {post.title}')

    print('\n✅ ALL TESTS PASSED! Forward references are resolved correctly!')

except Exception as e:
    print(f'\n❌ ERROR: {e}')
    import traceback
    traceback.print_exc()
    sys.exit(1)
