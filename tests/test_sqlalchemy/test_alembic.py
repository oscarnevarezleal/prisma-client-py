"""The emitted Alembic baseline has to adopt a real database, not just parse.

`docs/prisma-to-sqlalchemy-runbook.md` §3 is the manual version of this: run
`alembic init`, hand-edit `env.py`, autogenerate, and check the diff is empty.
These tests run the generated project against PostgreSQL and check the same
things the runbook asks a human to check --- plus the one it warns about, that an
empty autogenerate diff is a *weaker* signal than DDL equality.
"""

from __future__ import annotations

import os
import sys
import shutil
import subprocess
from typing import Any, Dict, Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa

from prisma.sa._alembic import PRISMA_MIGRATIONS_TABLE, build_alembic_baseline
from prisma.sa._declarative import build_declarative

REPO_ROOT = Path(__file__).resolve().parents[2]
URL = os.environ.get('PRISMA_SA_TEST_DATABASE_URL')

needs_alembic = pytest.mark.skipif(
    URL is None or shutil.which('alembic') is None,
    reason='PRISMA_SA_TEST_DATABASE_URL is not set, or alembic is not installed',
)
needs_ruff = pytest.mark.skipif(shutil.which('ruff') is None, reason='ruff is not installed')


# -- fixtures ----------------------------------------------------------------


@pytest.fixture(scope='module', name='baseline')
def baseline_fixture(generated: Dict[str, Any]) -> Any:
    return build_alembic_baseline(generated['schema'], generated['enums'], generated['provider'])


@pytest.fixture(name='project')
def project_fixture(generated: Dict[str, Any], tmp_path: Path) -> Path:
    """A complete Alembic project, models included, ready to run."""
    baseline = build_alembic_baseline(
        generated['schema'],
        generated['enums'],
        generated['provider'],
        models_import='models',
    )
    for relative, content in baseline.files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    emitted = build_declarative(generated['schema'], generated['enums'], generated['provider'])
    (tmp_path / 'models.py').write_text(emitted.source)
    return tmp_path


@pytest.fixture(name='database')
def database_fixture(request: pytest.FixtureRequest) -> Iterator[str]:
    """A scratch database, empty unless the test asks for the schema.

    Built with `MetaData.create_all()` rather than `prisma db push`:
    `test_ddl_equivalence.py` already proves the two produce the same database,
    and running the CLI here would double a slow fixture to prove it again.
    """
    assert URL is not None
    base, _, name = URL.rstrip('/').rpartition('/')
    scratch = f'{name}_prisma_alembic_{abs(hash(request.node.name)) % 10000}'

    admin(URL, f'DROP DATABASE IF EXISTS "{scratch}"')
    admin(URL, f'CREATE DATABASE "{scratch}"')
    try:
        yield f'{base}/{scratch}'
    finally:
        admin(URL, f'DROP DATABASE IF EXISTS "{scratch}"')


def admin(url: str, statement: str) -> None:
    engine = sa.create_engine(driver_url(url), isolation_level='AUTOCOMMIT')
    with engine.connect() as conn:
        conn.execute(sa.text(statement))
    engine.dispose()


def driver_url(url: str) -> str:
    _, _, rest = url.partition('://')
    return f'postgresql+psycopg://{rest}'


def engine_for(url: str) -> sa.Engine:
    return sa.create_engine(driver_url(url))


def build_schema(url: str, metadata: sa.MetaData) -> None:
    engine = engine_for(url)
    try:
        metadata.create_all(engine)
    finally:
        engine.dispose()


def alembic(project: Path, url: str, *args: str) -> 'subprocess.CompletedProcess[str]':
    return subprocess.run(
        [sys.executable, '-m', 'alembic', '-c', 'alembic.ini', *args],
        cwd=project,
        capture_output=True,
        text=True,
        env={**os.environ, 'DATABASE_URL': driver_url(url), 'PYTHONPATH': str(REPO_ROOT / 'src')},
    )


def upgrade_body(path: Path) -> str:
    text = path.read_text()
    start = text.index('def upgrade()')
    return text[start : text.index('def downgrade()')]


def table_names(url: str) -> set[str]:
    engine = engine_for(url)
    try:
        return set(sa.inspect(engine).get_table_names())
    finally:
        engine.dispose()


# -- the emitted files -------------------------------------------------------


def test_emits_a_complete_project(baseline: Any) -> None:
    assert sorted(baseline.files) == [
        'alembic.ini',
        'migrations/env.py',
        'migrations/script.py.mako',
        f'migrations/versions/{baseline.revision}_baseline.py',
    ]


def test_revision_is_derived_from_the_schema(generated: Dict[str, Any], baseline: Any) -> None:
    """Regenerating rewrites the baseline instead of adding a second one.

    A timestamp-derived id would leave two baselines, neither of which knows
    about the other, and `alembic upgrade head` would then have two heads.
    """
    again = build_alembic_baseline(generated['schema'], generated['enums'], generated['provider'])
    assert again.revision == baseline.revision
    assert again.files == baseline.files

    smaller = {'Account': generated['schema']['Account']}
    assert build_alembic_baseline(smaller, generated['enums'], generated['provider']).revision != baseline.revision


def test_baseline_lists_every_table(baseline: Any, metadata: sa.MetaData) -> None:
    assert sorted(baseline.tables) == sorted(table.name for table in metadata.tables.values())
    assert '_EntryToLabel' in baseline.tables, 'the implicit join tables have no model but do have DDL'


def test_baseline_creates_nothing(baseline: Any) -> None:
    """The cutover case: Prisma built the tables, Alembic only adopts them."""
    revision = baseline.files[f'migrations/versions/{baseline.revision}_baseline.py']
    for operation in ('create_table', 'drop_table', 'create_index', 'add_column', 'alter_column'):
        assert f'op.{operation}' not in revision, operation


def test_baseline_refuses_to_stamp_a_database_that_was_never_built(baseline: Any) -> None:
    """Not a bare `pass`.

    Stamping an empty database as migrated is silent, and every later revision
    then runs against a schema that is not there.
    """
    revision = baseline.files[f'migrations/versions/{baseline.revision}_baseline.py']
    assert 'get_table_names' in revision
    assert 'raise RuntimeError' in revision


def test_env_leaves_prisma_bookkeeping_alone(baseline: Any) -> None:
    """`_prisma_migrations` is not in the metadata, so autogenerate drops it."""
    env = baseline.files['migrations/env.py']
    assert f'name == {PRISMA_MIGRATIONS_TABLE!r}' in env
    assert 'include_object=include_object' in env


def test_env_turns_on_the_comparisons_that_are_off_by_default(baseline: Any) -> None:
    env = baseline.files['migrations/env.py']
    assert env.count('compare_type=True') == 2  # offline and online
    assert env.count('compare_server_default=compare_server_default') == 2


def test_env_keeps_the_runbook_warning(baseline: Any) -> None:
    """An empty autogenerate diff is necessary, not sufficient.

    Dropping this warning is how the weaker check gets mistaken for the gate.
    """
    env = baseline.files['migrations/env.py']
    assert 'WEAKER check than DDL' in env
    for ignored in ('ondelete', 'constraint and index names', 'column order', 'sequences', 'index methods'):
        assert ignored in env, ignored
    assert 'pg_dump --schema-only' in env


def test_env_warns_about_running_both_migration_tools(baseline: Any) -> None:
    env = baseline.files['migrations/env.py']
    assert 'Do not run `prisma migrate` and `alembic upgrade` against the same database' in env


def test_target_metadata_defaults_to_the_core_layer(baseline: Any) -> None:
    assert 'from prisma import sa' in baseline.files['migrations/env.py']
    assert 'target_metadata = sa.metadata()' in baseline.files['migrations/env.py']


def test_target_metadata_can_point_at_generated_models(generated: Dict[str, Any]) -> None:
    baseline = build_alembic_baseline(
        generated['schema'],
        generated['enums'],
        generated['provider'],
        models_import='myapp.models',
    )
    env = baseline.files['migrations/env.py']
    assert 'from myapp.models import Base' in env
    assert 'target_metadata = Base.metadata' in env


def test_script_location_is_configurable(generated: Dict[str, Any]) -> None:
    baseline = build_alembic_baseline(
        generated['schema'],
        generated['enums'],
        generated['provider'],
        script_location='db/alembic',
    )
    assert 'db/alembic/env.py' in baseline.files
    assert 'script_location = db/alembic' in baseline.files['alembic.ini']


@needs_ruff
def test_emitted_python_is_formatted_and_lints(baseline: Any, tmp_path: Path) -> None:
    """Including the unused-argument rule.

    Alembic's hooks take five positional arguments and these read two of them,
    so the rest are underscored --- otherwise the file arrives in someone's
    project already failing their lint.
    """
    config = str(REPO_ROOT / 'pyproject.toml')
    for relative, content in baseline.files.items():
        if not relative.endswith('.py'):
            continue
        path = tmp_path / Path(relative).name
        path.write_text(content)
        for command in (['format', '--check'], ['check', '--select', 'ARG,F401,I,B,E722']):
            result = subprocess.run(
                ['ruff', *command, '--config', config, str(path)],
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0, f'{relative} ({command[0]}):\n{result.stdout}\n{result.stderr}'


def test_emitted_python_type_checks(project: Path) -> None:
    """Checked as a project, so `env.py`'s import of the models resolves."""
    result = subprocess.run(
        [
            sys.executable,
            '-m',
            'mypy',
            '--strict',
            '--cache-dir',
            str(project / '.mypy'),
            str(project / 'models.py'),
            str(project / 'migrations' / 'env.py'),
        ],
        capture_output=True,
        text=True,
        cwd=project,
    )
    assert result.returncode == 0, f'mypy:\n{result.stdout}\n{result.stderr}'


# -- against a real database -------------------------------------------------


@needs_alembic
def test_upgrade_adopts_the_existing_database(project: Path, database: str, metadata: sa.MetaData) -> None:
    """The tables exist; the baseline must add only `alembic_version`."""
    build_schema(database, metadata)
    before = table_names(database)

    result = alembic(project, database, 'upgrade', 'head')
    assert result.returncode == 0, f'{result.stdout}\n{result.stderr}'

    assert table_names(database) - before == {'alembic_version'}

    engine = engine_for(database)
    try:
        with engine.connect() as conn:
            stamped = list(conn.execute(sa.text('SELECT version_num FROM alembic_version')))
    finally:
        engine.dispose()

    baseline = build_alembic_baseline({}, {}, 'postgresql')
    assert stamped != [], 'the baseline must record a version'
    assert stamped[0][0] != baseline.revision, 'the revision must be derived from this schema'


@needs_alembic
def test_autogenerate_diff_is_empty(project: Path, database: str, metadata: sa.MetaData) -> None:
    """The runbook's gate for this phase, automated."""
    build_schema(database, metadata)
    assert alembic(project, database, 'upgrade', 'head').returncode == 0

    result = alembic(project, database, 'revision', '--autogenerate', '-m', 'gate')
    assert result.returncode == 0, f'{result.stdout}\n{result.stderr}'

    (generated_file,) = [path for path in (project / 'migrations' / 'versions').iterdir() if 'gate' in path.name]
    body = upgrade_body(generated_file)
    assert 'op.' not in body, f'autogenerate is not empty:\n{body}'


@needs_alembic
def test_a_lost_sequence_default_is_still_reported(project: Path, database: str, metadata: sa.MetaData) -> None:
    """The false positive is suppressed; the real failure is not.

    A non-primary-key `@default(autoincrement())` reflects as a SERIAL, whose
    `DEFAULT nextval(...)` Alembic drops from the reflected column --- comparing
    that naively is a permanent spurious diff. Suppressing it must not also
    suppress a column that genuinely lost its default, which is the failure that
    makes every INSERT fail while autogenerate stays quiet.
    """
    build_schema(database, metadata)
    assert alembic(project, database, 'upgrade', 'head').returncode == 0

    engine = engine_for(database)
    try:
        with engine.begin() as conn:
            conn.execute(sa.text('ALTER TABLE "Ticket" ALTER COLUMN "ticketNumber" DROP DEFAULT'))
    finally:
        engine.dispose()

    result = alembic(project, database, 'revision', '--autogenerate', '-m', 'regression')
    assert result.returncode == 0, f'{result.stdout}\n{result.stderr}'

    (generated_file,) = [path for path in (project / 'migrations' / 'versions').iterdir() if 'regression' in path.name]
    body = upgrade_body(generated_file)
    assert 'ticketNumber' in body, f'a dropped sequence default must still be a diff:\n{body}'


@needs_alembic
def test_baseline_refuses_an_empty_database(project: Path, database: str) -> None:
    """Applying the baseline to a database nobody built is the silent failure."""
    result = alembic(project, database, 'upgrade', 'head')
    assert result.returncode != 0
    assert 'adopts a database Prisma already created' in result.stderr
    assert 'accounts' in result.stderr
    assert table_names(database) == set()
