"""`prisma py sqlalchemy` — tooling for migrating off Prisma onto SQLAlchemy.

Only `doctor` lives here. It reads the codebase and reports what a migration
would cost, measured against `docs/prisma-to-sqlalchemy-runbook.md`: §4.1 is the
set of translations that were executed against a live PostgreSQL database and
compared row for row, §5 is the set with no verified translation at all, and
anything in neither is reported as such rather than guessed at.

It changes nothing. It is the step before the runbook's Phase 2.
"""

from __future__ import annotations

import json as _json
from typing import List, Tuple, Optional
from pathlib import Path

import click

from ..utils import PathlibPath
from ...sa._doctor import to_json, render_text, diagnose_paths

#: Where a schema lives when nobody says. Prisma's own default first, then the
#: `prismaSchemaFolder` layout, which is a *directory* of `.prisma` files and
#: which §0 is explicit must be read in full rather than one file of it.
DEFAULT_SCHEMA_LOCATIONS = ('schema.prisma', 'prisma/schema.prisma', 'prisma')


@click.group('sqlalchemy')
def cli() -> None:
    """Migrate from Prisma Client Python to SQLAlchemy."""


@cli.command('doctor')
@click.option(
    '--json',
    'as_json',
    is_flag=True,
    default=False,
    help='Emit the same report as JSON, for a tool to consume.',
)
@click.option(
    '--schema',
    type=PathlibPath(exists=True),
    default=None,
    help='The Prisma schema file, or the folder of them under `prismaSchemaFolder`. '
    'Discovered from ./schema.prisma, ./prisma/schema.prisma or ./prisma when omitted.',
)
@click.option(
    '--path',
    'source_paths',
    type=PathlibPath(exists=True),
    multiple=True,
    help='A file or directory of Python source to scan. Repeatable; defaults to the working directory.',
)
def doctor(as_json: bool, schema: Optional[Path], source_paths: Tuple[Path, ...]) -> None:
    """Report what this codebase would cost to migrate to SQLAlchemy.

    Every call site is classified `verified` (a translation in §4.1 of the
    migration runbook, executed against a live database), `stop` (a row of §5,
    named), or `unknown` — neither, which is said out loud rather than rounded
    to either side.
    """
    root = Path.cwd()
    paths: List[Path] = list(source_paths) or [root]
    schema_paths = [schema] if schema is not None else _discover_schema(root)

    diagnosis = diagnose_paths(paths, root=root, schema_paths=schema_paths)

    if as_json:
        click.echo(_json.dumps(to_json(diagnosis), indent=2))
    else:
        click.echo(render_text(diagnosis), nl=False)


def _discover_schema(root: Path) -> List[Path]:
    for candidate in DEFAULT_SCHEMA_LOCATIONS:
        path = root / candidate
        if path.is_file():
            return [path]
        if path.is_dir() and any(path.glob('*.prisma')):
            return [path]
    return []
