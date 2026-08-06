"""The gate: our metadata must describe the database Prisma actually creates.

Runs `prisma db push` into one database and `MetaData.create_all()` into
another, then compares `pg_dump --schema-only` output. That is a stronger check
than an Alembic autogenerate diff, which is silent about foreign key actions,
constraint names, index methods, column ordering and CHECK constraints — every
one of which this package has to get right.

Needs a PostgreSQL server, the `prisma` CLI and `pg_dump`; skipped without them.
Point it at a database with::

    PRISMA_SA_TEST_DATABASE_URL=postgres://user:pass@127.0.0.1:5432/postgres \\
        pytest tests/test_sqlalchemy

The two scratch databases it creates and drops are named after the URL's
database with `_prisma_truth` / `_prisma_sa` appended.
"""

from __future__ import annotations

import os
import sys
import shutil
import difflib
import subprocess
from typing import Any, Dict, List, Tuple, Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa

from ..dmmf_sample import SAMPLE

SCHEMA = SAMPLE.parent / 'dmmf_wire_sample.prisma'
URL = os.environ.get('PRISMA_SA_TEST_DATABASE_URL')

pytestmark = [
    pytest.mark.skipif(URL is None, reason='PRISMA_SA_TEST_DATABASE_URL is not set'),
    pytest.mark.skipif(shutil.which('pg_dump') is None, reason='pg_dump is not installed'),
    pytest.mark.skipif(not SCHEMA.exists(), reason=f'{SCHEMA} is missing'),
]

IGNORED_DUMP_PREFIXES = (
    '--',
    # pg_dump 16+ wraps output in a per-run nonce
    '\\restrict',
    '\\unrestrict',
)


def _split_url(url: str) -> Tuple[str, str]:
    base, _, database = url.rstrip('/').rpartition('/')
    return base, database


def _sqlalchemy_url(url: str) -> str:
    scheme, _, rest = url.partition('://')
    assert scheme in ('postgres', 'postgresql'), f'not a PostgreSQL URL: {url!r}'
    return f'postgresql+psycopg://{rest}'


def _recreate(admin_url: str, name: str) -> None:
    engine = sa.create_engine(_sqlalchemy_url(admin_url), isolation_level='AUTOCOMMIT')
    with engine.connect() as conn:
        conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}"'))
        conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    engine.dispose()


def _drop(admin_url: str, name: str) -> None:
    engine = sa.create_engine(_sqlalchemy_url(admin_url), isolation_level='AUTOCOMMIT')
    with engine.connect() as conn:
        conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}"'))
    engine.dispose()


def _dump(url: str) -> List[str]:
    out = subprocess.run(
        ['pg_dump', '--schema-only', '--no-owner', '--no-acl', '--no-comments', url],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    _, database = _split_url(url)
    return [
        line.replace(database, '<db>')
        for line in out.splitlines()
        if line.strip() and not line.startswith(IGNORED_DUMP_PREFIXES)
    ]


@pytest.fixture(scope='module', name='databases')
def databases_fixture(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Dict[str, str]]:
    assert URL is not None
    base, database = _split_url(URL)
    truth = f'{database}_prisma_truth'
    ours = f'{database}_prisma_sa'

    _recreate(URL, truth)
    _recreate(URL, ours)

    # `prisma db push` needs the schema to point at the scratch database; the
    # recorded schema reads its URL from an env var precisely so this works.
    workdir = tmp_path_factory.mktemp('prisma-sa')
    (workdir / 'schema.prisma').write_text(SCHEMA.read_text())

    result = subprocess.run(
        [sys.executable, '-m', 'prisma', 'db', 'push', '--schema=schema.prisma', '--skip-generate'],
        cwd=workdir,
        capture_output=True,
        text=True,
        env={**os.environ, 'BENCH_DATABASE_URL': f'{base}/{truth}'},
    )
    # asserted rather than skipped: this fixture exists to build the database
    # the comparison runs against, so a push that fails has nothing left to test
    assert result.returncode == 0, f'prisma db push failed:\n{result.stdout}\n{result.stderr}'

    try:
        yield {'base': base, 'truth': f'{base}/{truth}', 'ours': f'{base}/{ours}'}
    finally:
        _drop(URL, truth)
        _drop(URL, ours)


def test_ddl_is_identical(databases: Dict[str, str], generated: Dict[str, Any]) -> None:
    from prisma.sa import build_metadata

    metadata = build_metadata(generated['schema'], generated['enums'], 'postgresql')

    engine = sa.create_engine(_sqlalchemy_url(databases['ours']))
    try:
        metadata.create_all(engine)
    finally:
        engine.dispose()

    truth = _dump(databases['truth'])
    ours = _dump(databases['ours'])

    # the diff is only built when the assertion is about to fail
    assert truth == ours, 'DDL differs from what Prisma creates:\n' + '\n'.join(
        difflib.unified_diff(truth, ours, 'prisma', 'prisma.sa', lineterm='')
    )

    # guard against the comparison silently passing on two empty dumps
    assert sum(1 for line in truth if line.startswith(('CREATE', 'ALTER'))) > 20


def test_reference_schema_covers_the_hard_shapes(generated: Dict[str, Any]) -> None:
    """The DDL comparison is only as good as the schema it runs against."""
    schema = generated['schema']
    shapes = {rel['shape'] for spec in schema.values() for rel in spec['relations'].values()}
    assert shapes == {'to-one-owner', 'to-one-inverse', 'to-many', 'many-to-many'}

    # comprehensions rather than `any(...)`: a generator that short-circuits on
    # the first hit never runs its loop to exhaustion, which leaves the arc out
    # of the loop unexecuted and the coverage gate red
    assert [model for model, spec in schema.items() if spec['table'] != model], '@@map'
    assert [
        name for spec in schema.values() for name, field in spec['fields'].items() if field['column'] != name
    ], '@map'
    assert [spec for spec in schema.values() if len(spec['primary_key']['columns']) > 1], '@@id'
    # ...and one whose declared order is *not* the field order, which is the
    # only shape where the column flags cannot express the key
    assert [
        spec
        for spec in schema.values()
        if spec['primary_key']['columns']
        != [f['column'] for f in spec['fields'].values() if f['column'] in set(spec['primary_key']['columns'])]
    ], 'reordered @@id'
    indexes = [index for spec in schema.values() for index in spec['indexes']]
    assert [
        field for index in indexes for field in index['fields'] if field['operator_class']
    ], 'index operator classes'
    assert [field for index in indexes for field in index['fields'] if field['sort_order']], 'index sort order'
    assert [
        field
        for spec in schema.values()
        for unique in spec['uniques']
        for field in unique['field_modifiers']
        if field['sort_order']
    ], 'unique sort order'
    assert [
        rel for spec in schema.values() for rel in spec['relations'].values() if rel.get('join_ambiguous')
    ], 'self-referential m2m'
    assert generated['enums'], 'enums'


def test_no_stray_scratch_paths_in_repo() -> None:
    """The recorded schema must generate into a scratch dir, not the package.

    `output` defaults to the installed `prisma` package, so re-recording without
    it silently overwrites the dev client.
    """
    assert 'output' in SCHEMA.read_text()
    assert not Path(SCHEMA.parent / 'client').exists()
