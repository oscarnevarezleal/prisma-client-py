#!/usr/bin/env python
import os
os.environ['PRISMA_GENERATOR_INVOCATION'] = '1'

from prisma.models import TestUser

# Create a partial type
TestUser.create_partial(
    'TestUserWithoutRelations',
    exclude_relational_fields=True
)

print("✓ Partial type created successfully")
