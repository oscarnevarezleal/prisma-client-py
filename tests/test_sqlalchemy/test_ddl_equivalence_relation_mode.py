"""The same gate, for a datasource that sets `relationMode = "prisma"`.

`test_ddl_equivalence.py` proves `build_metadata()` describes the database
`prisma db push` creates. It cannot cover this case: `relationMode` is a
*datasource* setting and a schema has exactly one datasource, so the reference
schema there is `foreignKeys` and always will be. This runs the identical
comparison against a second recorded schema whose datasource is in the other
mode.

What that mode changes, measured rather than read from documentation:

* **no foreign key constraints are created at all** — Prisma enforces relations
  in the query engine, which is how PlanetScale and Vitess users run. Emitting
  them describes a database that does not exist, and every one is an
  `alembic revision --autogenerate` diff against a table that is otherwise fine;
* **no index is created in their place.** Prisma warns that relation scalars go
  unindexed and leaves it at that. An emitter that adds one to be helpful fails
  this gate, which is why the reference schema carries both an unindexed
  relation scalar and an explicitly indexed one;
* the implicit many-to-many join table loses its two foreign keys too, while
  keeping both of its indexes.

Needs the same PostgreSQL, `prisma` CLI and `pg_dump` as its sibling; the
scratch databases are named apart so the two can share one server.
"""

from __future__ import annotations

import os
import sys
import shutil
import difflib
import subprocess
from typing import Any, Dict, List, Iterator

import pytest
import sqlalchemy as sa

from ..dmmf_sample import RELATION_MODE_SAMPLE
from .test_ddl_equivalence import URL, _drop, _dump, _recreate, _split_url, _sqlalchemy_url

SCHEMA = RELATION_MODE_SAMPLE.parent / 'relation_mode_sample.prisma'

pytestmark = [
    pytest.mark.skipif(URL is None, reason='PRISMA_SA_TEST_DATABASE_URL is not set'),
    pytest.mark.skipif(shutil.which('pg_dump') is None, reason='pg_dump is not installed'),
    pytest.mark.skipif(not SCHEMA.exists(), reason=f'{SCHEMA} is missing'),
]


@pytest.fixture(scope='module', name='databases')
def databases_fixture(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Dict[str, str]]:
    assert URL is not None
    base, database = _split_url(URL)
    truth = f'{database}_relmode_truth'
    ours = f'{database}_relmode_sa'

    _recreate(URL, truth)
    _recreate(URL, ours)

    workdir = tmp_path_factory.mktemp('prisma-sa-relmode')
    (workdir / 'schema.prisma').write_text(SCHEMA.read_text())

    result = subprocess.run(
        [sys.executable, '-m', 'prisma', 'db', 'push', '--schema=schema.prisma', '--skip-generate'],
        cwd=workdir,
        capture_output=True,
        text=True,
        env={**os.environ, 'BENCH_DATABASE_URL': f'{base}/{truth}'},
    )
    assert result.returncode == 0, f'prisma db push failed:\n{result.stdout}\n{result.stderr}'

    try:
        yield {'base': base, 'truth': f'{base}/{truth}', 'ours': f'{base}/{ours}'}
    finally:
        _drop(URL, truth)
        _drop(URL, ours)


def test_ddl_is_identical(databases: Dict[str, str], relation_mode_metadata: sa.MetaData) -> None:
    engine = sa.create_engine(_sqlalchemy_url(databases['ours']))
    try:
        relation_mode_metadata.create_all(engine)
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


def test_prisma_really_creates_no_foreign_keys(databases: Dict[str, str]) -> None:
    """The premise, asserted against the database rather than assumed.

    If a future Prisma release started emitting constraints under this mode, the
    comparison above would still pass --- both sides would simply be wrong
    together, which is the failure mode this project keeps rediscovering.
    """
    truth = _dump(databases['truth'])
    assert [line for line in truth if 'FOREIGN KEY' in line] == []
    assert [line for line in truth if line.startswith('CREATE TABLE')], 'the dump is not empty'


def test_prisma_creates_no_index_for_an_unindexed_relation_scalar(databases: Dict[str, str]) -> None:
    """`members.tenant_id` has neither a constraint nor an index.

    Prisma only warns about it. Emitting an index here instead of the missing
    foreign key looks like the helpful thing to do and is a diff on every such
    column.
    """
    truth = _dump(databases['truth'])
    indexed = [line for line in truth if line.startswith(('CREATE INDEX', 'CREATE UNIQUE INDEX'))]

    assert [line for line in indexed if 'tenant_id' in line and 'members' in line] == []
    # the declared one is still there, so this is not just "no indexes at all"
    assert [line for line in indexed if 'member_manager_idx' in line]


def test_reference_schema_covers_the_hard_shapes(relation_mode_generated: Dict[str, Any]) -> None:
    """The DDL comparison is only as good as the schema it runs against."""
    schema = relation_mode_generated['schema']
    shapes = {rel['shape'] for spec in schema.values() for rel in spec['relations'].values()}
    assert shapes == {'to-one-owner', 'to-one-inverse', 'to-many', 'many-to-many'}

    # comprehensions rather than `any(...)`: a generator that short-circuits on
    # the first hit leaves the loop-exit arc unexecuted and the coverage gate red
    assert [model for model, spec in schema.items() if spec['table'] != model], '@@map'
    assert [
        name for spec in schema.values() for name, field in spec['fields'].items() if field['column'] != name
    ], '@map'
    assert [spec for spec in schema.values() if len(spec['primary_key']['columns']) > 1], '@@id'
    assert [
        rel for spec in schema.values() for rel in spec['relations'].values() if len(rel['fk_columns']) > 1
    ], 'compound foreign key'
    assert [
        rel for spec in schema.values() for rel in spec['relations'].values() if rel['fk_name'] == 'config_tenant_fk'
    ], '@relation(map:)'
    assert relation_mode_generated['enums'], 'enums'

    modes = {rel['relation_mode'] for spec in schema.values() for rel in spec['relations'].values()}
    assert modes == {'prisma'}


def test_the_two_reference_schemas_are_in_different_modes(generated: Dict[str, Any]) -> None:
    """Otherwise this file is a slower copy of its sibling."""
    modes = {rel['relation_mode'] for spec in generated['schema'].values() for rel in spec['relations'].values()}
    assert modes == {None}


def test_no_stray_scratch_paths_in_repo() -> None:
    """The recorded schema must generate into a scratch dir, not the package."""
    assert 'output' in SCHEMA.read_text()
    assert not (SCHEMA.parent / 'client').exists()


def test_metadata_carries_no_foreign_keys(relation_mode_metadata: sa.MetaData) -> None:
    """The Core side of the same claim, without needing a database."""
    constraints: List[Any] = [
        constraint
        for table in relation_mode_metadata.tables.values()
        for constraint in table.constraints
        if isinstance(constraint, sa.ForeignKeyConstraint)
    ]
    assert constraints == []
    assert len(relation_mode_metadata.tables) > 5
