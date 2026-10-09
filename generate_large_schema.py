#!/usr/bin/env python3
"""
Script to generate a Prisma schema with many models for memory testing.
"""

def generate_large_schema(num_models: int = 200) -> str:
    """Generate a Prisma schema with the specified number of models."""

    schema = """datasource db {
  provider = "sqlite"
  url      = "file:./dev.db"
}

generator client {
  provider = "prisma-client-py"
}

"""

    # Generate models
    for i in range(num_models):
        model_name = f"Model{i}"
        schema += f"""model {model_name} {{
  id          Int      @id @default(autoincrement())
  createdAt   DateTime @default(now())
  updatedAt   DateTime @updatedAt
  name        String
  description String?
  isActive    Boolean  @default(true)
  count       Int      @default(0)
  price       Float?
  data        String?
}}

"""

    return schema

if __name__ == "__main__":
    import sys

    num_models = int(sys.argv[1]) if len(sys.argv) > 1 else 200

    schema = generate_large_schema(num_models)

    with open("schema.prisma", "w") as f:
        f.write(schema)

    print(f"✓ Generated schema with {num_models} models")
    print(f"✓ Schema file size: {len(schema)} bytes")
