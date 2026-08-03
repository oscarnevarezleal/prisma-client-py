"""Fixtures for tests that actually write rows.

Everything here exists so a write test can be destructive without leaking into
the next one, and so a claim about what Prisma does can be checked against
Prisma rather than remembered.

Two databases are involved and they are the same database: one scratch database
built by `prisma db push` from the recorded reference schema, reached both
through a `sqlalchemy.Connection` (`connection`) and through a generated Prisma
client (`prisma_client`). The point of a write test here is to compare the two.

    PRISMA_SA_TEST_DATABASE_URL=postgres://user:pass@127.0.0.1:5432/postgres \\
        PYTEST_PLUGINS=pytester pytest tests/test_sqlalchemy/writes

Without that variable every test that reaches for one of these fixtures is
skipped, the same way `test_ddl_equivalence.py` skips. Tests here that need no
database still run.

Isolation
---------

`connection` runs inside a transaction that is **rolled back**, so nothing it
writes survives the test. The Prisma client cannot join that transaction — it is
a separate process with its own connection — so `prisma_client` instead
truncates every table on *setup*. Truncating on setup rather than teardown means
a test starts clean no matter how the previous one failed.

Consequence worth knowing before you write a test: rows written through
`prisma_client` are **not visible** to `connection` and vice versa, because the
`connection` transaction is uncommitted. A test that needs both to see the same
row must write it through the one that reads it, or commit deliberately.
"""

from __future__ import annotations

import os
import sys
import shutil
import subprocess
from typing import TYPE_CHECKING, Any, List, Tuple, Iterator
from pathlib import Path

import pytest

from ...dmmf_sample import SAMPLE

if TYPE_CHECKING:
    import sqlalchemy as sa

sqlalchemy = pytest.importorskip('sqlalchemy', reason='prisma[sqlalchemy] is not installed')

#: The recorded reference schema. It is the one the DDL gate uses, so the
#: database these tests write into is the database that gate proves we describe.
SCHEMA = SAMPLE.parent / 'dmmf_wire_sample.prisma'

DATABASE_URL = os.environ.get('PRISMA_SA_TEST_DATABASE_URL')

#: Attach to a module with `pytestmark = requires_database`. Applied
#: automatically to any test that requests a live fixture (see
#: `pytest_collection_modifyitems`); exported so a module can be explicit, or
#: mark a test that reaches the database without going through a fixture.
requires_database = [
    pytest.mark.skipif(DATABASE_URL is None, reason='PRISMA_SA_TEST_DATABASE_URL is not set'),
    pytest.mark.skipif(shutil.which('psql') is None, reason='the PostgreSQL client is not installed'),
    pytest.mark.skipif(not SCHEMA.exists(), reason=f'{SCHEMA} is missing'),
]

#: The generated client is imported by this name. Deliberately not `client` or
#: `prisma`: it goes on `sys.path` and must not shadow either.
CLIENT_PACKAGE = 'prisma_sa_write_client'

#: Requesting any of these means the test needs the scratch database.
LIVE_FIXTURES = frozenset(
    {
        'write_database_url',
        'write_engine',
        'write_metadata',
        'connection',
        'prisma_client',
        'installed_write_schema',
    }
)

_HERE = Path(__file__).parent


def pytest_collection_modifyitems(
    session: pytest.Session,
    config: pytest.Config,
    items: List[pytest.Item],
) -> None:
    """Mark anything reaching for a live fixture, so nobody has to remember to.

    A write test that quietly passes because it never reached a database is
    worse than no test. Applied by fixture use rather than to the whole package
    so the offline tests here — the refusals, the value shapes — keep running
    without a database.
    """
    for item in items:
        try:
            path = Path(str(item.fspath))
        except Exception:  # pragma: no cover - defensive, fspath is always set
            continue
        if _HERE not in path.parents:
            continue
        if not LIVE_FIXTURES.intersection(getattr(item, 'fixturenames', ())):
            continue
        for mark in requires_database:
            item.add_marker(mark)


def _split_url(url: str) -> Tuple[str, str]:
    base, _, database = url.rstrip('/').rpartition('/')
    return base, database


def sqlalchemy_url(url: str) -> str:
    """`postgres://` -> the driver actually installed here."""
    for prefix in ('postgres://', 'postgresql://'):
        if url.startswith(prefix):
            return 'postgresql+psycopg://' + url[len(prefix) :]
    return url


def _admin(url: str, *statements: str) -> None:
    engine = sqlalchemy.create_engine(sqlalchemy_url(url), isolation_level='AUTOCOMMIT')
    try:
        with engine.connect() as conn:
            for statement in statements:
                conn.execute(sqlalchemy.text(statement))
    finally:
        engine.dispose()


@pytest.fixture(scope='session', name='write_database_url')
def write_database_url_fixture(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A scratch database built from the reference schema, plus a client for it.

    `prisma db push` creates the tables *and* generates the Prisma client that
    `prisma_client` imports — one subprocess for both, once per session.
    """
    if DATABASE_URL is None:  # pragma: no cover - the marker skips first
        pytest.skip('PRISMA_SA_TEST_DATABASE_URL is not set')

    base, database = _split_url(DATABASE_URL)
    name = f'{database}_prisma_sa_writes'
    url = f'{base}/{name}'

    _admin(DATABASE_URL, f'DROP DATABASE IF EXISTS "{name}"')
    _admin(DATABASE_URL, f'CREATE DATABASE "{name}"')

    workdir = tmp_path_factory.mktemp('prisma-sa-writes')
    # The recorded schema generates an async client; these tests are sync, and
    # the interface is the only thing that has to change to run them.
    text = SCHEMA.read_text()
    text = text.replace('interface                   = "asyncio"', 'interface                   = "sync"')
    text = text.replace(
        'output                      = "./client"', f'output                      = "./{CLIENT_PACKAGE}"'
    )
    (workdir / 'schema.prisma').write_text(text)

    result = subprocess.run(
        [sys.executable, '-m', 'prisma', 'db', 'push', '--schema=schema.prisma'],
        cwd=workdir,
        capture_output=True,
        text=True,
        env={**os.environ, 'BENCH_DATABASE_URL': url},
    )
    if result.returncode != 0:
        _admin(DATABASE_URL, f'DROP DATABASE IF EXISTS "{name}"')
        pytest.skip(f'prisma db push failed:\n{result.stdout}\n{result.stderr}')

    sys.path.insert(0, str(workdir))
    try:
        yield url
    finally:
        sys.path.remove(str(workdir))
        sys.modules.pop(CLIENT_PACKAGE, None)
        _admin(DATABASE_URL, f'DROP DATABASE IF EXISTS "{name}"')


@pytest.fixture(scope='session', name='write_engine')
def write_engine_fixture(write_database_url: str) -> Iterator['sa.Engine']:
    engine = sqlalchemy.create_engine(sqlalchemy_url(write_database_url))
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(scope='session', name='write_metadata')
def write_metadata_fixture(write_database_url: str) -> 'sa.MetaData':
    """`MetaData` built from the *generated* client's schema payload.

    Not `prisma.sa.metadata()`: that reads the installed dev client, which is
    generated from a different schema and against SQLite.
    """
    from prisma.sa import build_metadata

    client = _import_client()
    return build_metadata(client.metadata.SCHEMA, client.metadata.ENUM_SCHEMA, 'postgresql')


@pytest.fixture(name='connection')
def connection_fixture(write_engine: 'sa.Engine') -> Iterator['sa.Connection']:
    """A live connection inside a transaction that is rolled back afterwards."""
    with write_engine.connect() as conn:
        transaction = conn.begin()
        try:
            yield conn
        finally:
            transaction.rollback()


def _import_client() -> Any:
    import importlib

    return importlib.import_module(CLIENT_PACKAGE)


@pytest.fixture(scope='session', name='_prisma_client_session')
def prisma_client_session_fixture(write_database_url: str) -> Iterator[Any]:
    client = _import_client()
    db = client.Prisma(datasource={'url': write_database_url})
    db.connect()
    try:
        yield db
    finally:
        db.disconnect()


@pytest.fixture(name='prisma_client')
def prisma_client_fixture(
    _prisma_client_session: Any,
    write_database_url: str,
) -> Iterator[Any]:
    """A connected Prisma client against the same database as `connection`.

    Every table is truncated on setup, so what this client commits cannot leak
    into the next test.
    """
    _truncate(write_database_url)
    yield _prisma_client_session


@pytest.fixture(name='installed_write_schema')
def installed_write_schema_fixture(write_database_url: str) -> Iterator[None]:
    """Point `prisma._schema` at the generated write client's schema.

    `values_for_create` and friends read `prisma.metadata`, which in this
    checkout belongs to the dev client. Without this the model names in a write
    test would not be the ones in the database it is writing to.
    """
    import prisma.metadata
    from prisma import sa as prisma_sa

    client = _import_client()

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(prisma.metadata, 'SCHEMA', client.metadata.SCHEMA, raising=False)
        patch.setattr(prisma.metadata, 'ENUM_SCHEMA', client.metadata.ENUM_SCHEMA, raising=False)
        patch.setattr(prisma.metadata, 'DATABASE_PROVIDER', 'postgresql', raising=False)
        prisma_sa.clear_cache()
        try:
            yield
        finally:
            prisma_sa.clear_cache()


def _truncate(url: str) -> None:
    engine = sqlalchemy.create_engine(sqlalchemy_url(url), isolation_level='AUTOCOMMIT')
    try:
        with engine.connect() as conn:
            tables = conn.execute(
                sqlalchemy.text(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename <> '_prisma_migrations'"
                )
            ).scalars()
            names = ', '.join(f'"{table}"' for table in tables)
            if names:
                conn.execute(sqlalchemy.text(f'TRUNCATE {names} RESTART IDENTITY CASCADE'))
    finally:
        engine.dispose()
