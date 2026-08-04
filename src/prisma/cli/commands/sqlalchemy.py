"""`prisma py sqlalchemy` — tooling for migrating off Prisma onto SQLAlchemy.

Three commands, in the order you would run them:

- `doctor` reads the codebase and reports what a migration would cost, measured
  against `docs/prisma-to-sqlalchemy-runbook.md`: §4.1 is the set of
  translations that were executed against a live PostgreSQL database and
  compared row for row, §5 is the set with no verified translation at all, and
  anything in neither is reported as such rather than guessed at. It changes
  nothing.
- `generate` emits declarative ORM source for the schema.
- `baseline` emits an Alembic project that adopts the database Prisma already
  created, rather than trying to recreate it.

There is deliberately no command for the Core path. Keeping `MetaData` at
runtime needs no code generation at all — that is `prisma.sa.metadata()`, and a
generated module that only forwards to it would be a file to keep in sync for
no benefit.
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


@cli.command('generate')
@click.option(
    '--out',
    type=PathlibPath(),
    default=None,
    help='Write the models here instead of to stdout.',
)
@click.option(
    '--strict',
    is_flag=True,
    default=False,
    help='Fail instead of emitting a file with shapes left out.',
)
def generate(out: Optional[Path], strict: bool) -> None:
    """Emit declarative ORM models for the generated schema.

    Every DDL-affecting decision is read off the same `MetaData` that
    `prisma.sa.metadata()` builds, so the models describe the database Prisma
    made rather than an approximation of it.

    A shape that cannot be expressed faithfully is left out and reported rather
    than guessed at — pass `--strict` to make that an error instead.
    """
    from ...sa import get_schema, get_provider, get_enum_schema
    from ...sa._declarative import build_declarative

    emitted = build_declarative(get_schema(), get_enum_schema(), get_provider(), strict=strict)

    if out is None:
        click.echo(emitted.source, nl=False)
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(emitted.source, encoding='utf-8')
        click.echo(f'wrote {out}')

    # to stderr so that `--out -`-style piping of the source stays clean
    for refusal in emitted.refusals:
        click.echo(f'not emitted: {refusal.describe()}', err=True)


@cli.command('baseline')
@click.option(
    '--out',
    type=PathlibPath(),
    default=Path('migrations'),
    help='Directory to write the Alembic project into. Defaults to ./migrations.',
)
@click.option(
    '--models-import',
    default=None,
    help='Module holding the generated models, e.g. `myapp.models`. Points `env.py` at `prisma.sa.metadata()` when omitted.',
)
def baseline(out: Path, models_import: Optional[str]) -> None:
    """Emit an Alembic project that adopts the existing database.

    The baseline revision creates nothing. It asserts the tables are already
    there and stamps them as migrated, because the failure worth guarding
    against is stamping an *empty* database as up to date.
    """
    from ...sa import get_schema, get_provider, get_enum_schema
    from ...sa._alembic import build_alembic_baseline

    built = build_alembic_baseline(
        get_schema(),
        get_enum_schema(),
        get_provider(),
        script_location=out.name,
        models_import=models_import,
    )

    for relative, contents in built.files.items():
        # `files` keys are already relative to the project root and carry
        # `script_location` as their prefix, so they land beside `out`
        path = out.parent / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding='utf-8')
        click.echo(f'wrote {path}')

    click.echo(f'\nbaseline revision {built.revision} covers {len(built.tables)} tables')
    click.echo('run `alembic upgrade head` to stamp the database it describes')


def _discover_schema(root: Path) -> List[Path]:
    for candidate in DEFAULT_SCHEMA_LOCATIONS:
        path = root / candidate
        if path.is_file():
            return [path]
        if path.is_dir() and any(path.glob('*.prisma')):
            return [path]
    return []
