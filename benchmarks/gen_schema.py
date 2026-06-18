"""Generate a synthetic Prisma schema with a configurable number of models.

Each model gets a spread of scalar fields plus a chain relation to the next
model, so the generated client exercises both scalar and relationship handling
(the latter is what `scalar_fields_only` is meant to trim).
"""
from __future__ import annotations

import argparse
from pathlib import Path

SCALAR_FIELDS = """\
  f_str   String
  f_int   Int
  f_bool  Boolean
  f_float Float
  f_date  DateTime @default(now())
  f_opt   String?
"""


def build_schema(num_models: int, output: str, interface: str = "asyncio",
                 recursive_type_depth: int = 5) -> str:
    lines: list[str] = [
        "datasource db {",
        '  provider = "sqlite"',
        '  url      = "file:dev.db"',
        "}",
        "",
        "generator client {",
        '  provider             = "prisma-client-py"',
        f'  interface            = "{interface}"',
        f'  output               = "{output}"',
        f"  recursive_type_depth = {recursive_type_depth}",
        "}",
        "",
    ]

    for i in range(num_models):
        lines.append(f"model Model{i} {{")
        lines.append("  id Int @id @default(autoincrement())")
        lines.append(SCALAR_FIELDS.rstrip("\n"))
        # chain relation: Model{i}.next -> Model{i+1} (this side holds the FK),
        # Model{i+1}.prev is the back-reference list on relation "chain{i}".
        if i < num_models - 1:
            lines.append(f"  nextId Int?")
            lines.append(
                f'  next   Model{i + 1}? @relation("chain{i}", fields: [nextId], references: [id])'
            )
        if i > 0:
            lines.append(f'  prev   Model{i - 1}[] @relation("chain{i - 1}")')
        lines.append("}")
        lines.append("")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", type=int, required=True, help="number of models")
    parser.add_argument("--output", required=True, help="generator output dir (the prisma package path)")
    parser.add_argument("--schema", required=True, help="where to write the .prisma file")
    parser.add_argument("--interface", default="asyncio")
    parser.add_argument("--recursive-type-depth", type=int, default=5,
                        help="generator recursive_type_depth (-1 = true recursive types, Pyright only)")
    args = parser.parse_args()

    schema = build_schema(args.models, args.output, args.interface, args.recursive_type_depth)
    Path(args.schema).parent.mkdir(parents=True, exist_ok=True)
    Path(args.schema).write_text(schema)
    print(f"Wrote schema with {args.models} models -> {args.schema}")


if __name__ == "__main__":
    main()
