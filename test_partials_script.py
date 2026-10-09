#!/usr/bin/env python3
"""
Partial type generator script for testing.
This script is called during prisma generate to create custom partial types.
"""

print('🔧 Partial type generator starting...')

try:
    # Test importing models from prisma.models
    print('📦 Importing models...')
    from prisma.models import TestUser, TestPost
    print(f'   ✓ Imported TestUser: {TestUser}')
    print(f'   ✓ Imported TestPost: {TestPost}')

    # Create partial types
    print('🏗️  Creating partial types...')

    TestUser.create_partial(
        'TestUserBasic',
        exclude=['posts']
    )
    print('   ✓ Created TestUserBasic')

    TestUser.create_partial(
        'TestUserWithName',
        include=['id', 'name']
    )
    print('   ✓ Created TestUserWithName')

    TestPost.create_partial(
        'TestPostBasic',
        exclude=['author']
    )
    print('   ✓ Created TestPostBasic')

    TestPost.create_partial(
        'TestPostWithTitle',
        include=['id', 'title']
    )
    print('   ✓ Created TestPostWithTitle')

    print('✅ Partial type generator completed successfully!')

except Exception as e:
    print(f'❌ Error in partial type generator: {e}')
    import traceback
    traceback.print_exc()
    raise
