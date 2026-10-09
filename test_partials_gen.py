#!/usr/bin/env python3
"""Script to create partial types during generation."""

from prisma.models import TestUser, TestPost

# Create partial types
TestUser.create_partial(
    'TestUserBasic',
    exclude=['posts']
)

TestPost.create_partial(
    'TestPostBasic',
    exclude=['author']
)

print('✓ Partial types created successfully!')
