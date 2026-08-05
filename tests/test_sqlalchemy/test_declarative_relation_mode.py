"""Declarative models for a schema with no foreign keys in the database.

`relationship()` derives its join condition from `ForeignKey` metadata, and
under `relationMode = "prisma"` there is none --- so every relation has to say
what it joins on explicitly, or the mapping raises `NoForeignKeysError` at
`configure_mappers()`.

Compiling is not the bar. A join condition can be well-formed, type-check,
configure cleanly and still return the wrong rows --- the direction of a
self-relation and which column of a join table each side traverses are both
easy to get backwards and invisible to every static check. So the assertions
below load rows out of a live database and name which ones they expect.
"""

from __future__ import annotations

import sys
import shutil
import difflib
import subprocess
from typing import Any, Dict, List, Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import orm

from prisma.sa._declarative import build_declarative

from .test_declarative import (
    URL,
    REPO_ROOT,
    drop,
    dump,
    recreate,
    index_ddl,
    needs_ruff,
    import_source,
    needs_pyright,
    relationships,
    needs_database,
    sqlalchemy_url,
    create_table_ddl,
)


@pytest.fixture(scope='module', name='emitted')
def emitted_fixture(relation_mode_generated: Dict[str, Any]) -> Any:
    return build_declarative(
        relation_mode_generated['schema'],
        relation_mode_generated['enums'],
        relation_mode_generated['provider'],
    )


@pytest.fixture(scope='module', name='models')
def models_fixture(emitted: Any, tmp_path_factory: pytest.TempPathFactory) -> Any:
    return import_source(
        emitted.source,
        tmp_path_factory.mktemp('declarative-relmode') / 'models.py',
        'prisma_sa_relmode_models',
    )


# -- the emitted source ------------------------------------------------------


def test_nothing_is_refused(emitted: Any) -> None:
    assert emitted.refusals == ()


def test_no_foreign_key_constraints_are_declared(models: Any) -> None:
    constraints = [
        constraint
        for table in models.Base.metadata.tables.values()
        for constraint in table.constraints
        if isinstance(constraint, sa.ForeignKeyConstraint)
    ]
    assert constraints == []
    assert 'sa.ForeignKeyConstraint' not in emitted_source(models)


def emitted_source(models: Any) -> str:
    return Path(str(models.__file__)).read_text()


def test_join_conditions_are_spelled_out(emitted: Any) -> None:
    """`foreign()` is what says which side is the dependent one.

    Without a `ForeignKey` in the metadata there is nothing else to say it, and
    `foreign_keys=` alone only disambiguates among constraints that exist.
    """
    assert "primaryjoin='foreign(Member.tenantId) == Tenant.id'" in emitted.source
    assert "primaryjoin='foreign(Config.tenantId) == Tenant.id'" in emitted.source
    assert "primaryjoin='foreign(Member.managerId) == Member.id'" in emitted.source
    assert 'foreign_keys=' not in emitted.source


def test_compound_foreign_key_becomes_a_conjunction(emitted: Any) -> None:
    assert (
        "primaryjoin='and_(foreign(Seat.regionTenantId) == Region.tenantId, "
        "foreign(Seat.regionCode) == Region.code)'" in emitted.source
    )


def test_many_to_many_joins_through_the_join_table(emitted: Any) -> None:
    """The join table has no class, so these cannot be strings.

    `A` is whichever model sorts first by name --- `Group` here --- and getting
    that backwards silently returns the other side's rows.
    """
    assert "primaryjoin=lambda: Member.id == foreign(t_GroupToMember.c['B'])" in emitted.source
    assert "secondaryjoin=lambda: foreign(t_GroupToMember.c['A']) == Group.id" in emitted.source
    assert "primaryjoin=lambda: Group.id == foreign(t_GroupToMember.c['A'])" in emitted.source
    assert "secondaryjoin=lambda: foreign(t_GroupToMember.c['B']) == Member.id" in emitted.source
    assert 'from sqlalchemy.orm import Mapped, DeclarativeBase, foreign, relationship, mapped_column' in emitted.source


def test_declared_index_on_a_relation_scalar_survives(models: Any, relation_mode_metadata: sa.MetaData) -> None:
    """Nothing is emitted *for* the missing constraint; `@@index` still is."""
    assert index_ddl(models.Base.metadata, 'members') == index_ddl(relation_mode_metadata, 'members')
    assert [name for name in index_ddl(models.Base.metadata, 'members') if 'member_manager_idx' in name]
    assert [name for name in index_ddl(models.Base.metadata, 'members') if 'tenant_id' in name] == []


def test_create_table_ddl_matches_the_core_metadata(models: Any, relation_mode_metadata: sa.MetaData) -> None:
    names = sorted(relation_mode_metadata.tables)
    core = [create_table_ddl(relation_mode_metadata, name) for name in names]
    declarative = [create_table_ddl(models.Base.metadata, name) for name in names]

    assert core == declarative, 'declarative DDL differs from the Core metadata:\n' + '\n'.join(
        difflib.unified_diff('\n'.join(core).splitlines(), '\n'.join(declarative).splitlines(), 'core', 'declarative')
    )
    assert len(names) > 5


def test_mappers_configure(models: Any) -> None:
    orm.configure_mappers()
    assert sa.inspect(models.Member).local_table.name == 'members'


def test_relationship_directions(models: Any) -> None:
    assert relationships(models.Member)['tenant'].direction is orm.MANYTOONE
    assert relationships(models.Tenant)['members'].direction is orm.ONETOMANY
    assert relationships(models.Tenant)['config'].direction is orm.ONETOMANY
    assert relationships(models.Tenant)['config'].uselist is False
    assert relationships(models.Member)['manager'].direction is orm.MANYTOONE
    assert relationships(models.Member)['reports'].direction is orm.ONETOMANY
    assert relationships(models.Member)['groups'].direction is orm.MANYTOMANY
    assert relationships(models.Seat)['region'].direction is orm.MANYTOONE


# -- the emitted file has to survive the tools it will be checked with -------


@needs_ruff
def test_source_is_formatted_and_lints(emitted: Any, tmp_path: Path) -> None:
    path = tmp_path / 'models.py'
    path.write_text(emitted.source)
    config = str(REPO_ROOT / 'pyproject.toml')
    for command in (['format', '--check'], ['check']):
        result = subprocess.run(['ruff', *command, '--config', config, str(path)], capture_output=True, text=True)
        assert result.returncode == 0, f'ruff {command[0]}:\n{result.stdout}\n{result.stderr}'


def test_type_checks_with_mypy(emitted: Any, tmp_path: Path) -> None:
    """A `lambda` join condition is only usable if it type-checks strictly."""
    path = tmp_path / 'models.py'
    path.write_text(emitted.source)
    result = subprocess.run(
        [sys.executable, '-m', 'mypy', '--strict', '--cache-dir', str(tmp_path / '.mypy'), str(path)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, f'mypy:\n{result.stdout}\n{result.stderr}'


@needs_pyright
def test_type_checks_with_pyright(emitted: Any, tmp_path: Path) -> None:
    path = tmp_path / 'models.py'
    path.write_text(emitted.source)
    (tmp_path / 'pyrightconfig.json').write_text('{"typeCheckingMode": "strict", "pythonVersion": "3.9"}')
    result = subprocess.run(['pyright', str(path)], capture_output=True, text=True, cwd=tmp_path)
    assert result.returncode == 0, f'pyright:\n{result.stdout}\n{result.stderr}'


# -- the bar: the relationships load the right rows --------------------------

#: One tenant with two members --- `bo` reporting to `al` --- one config, two
#: groups, and a region with two seats. Deliberately asymmetric: a join
#: condition that traverses the wrong column, or a self-relation configured
#: backwards, returns a *different* set rather than an empty one.
ROWS = (
    "INSERT INTO tenants (id, tenant_name, tier) VALUES ('t1', 'Acme', 'FREE'), ('t2', 'Other', 'paid_tier')",
    "INSERT INTO configs (id, tenant_id, theme) VALUES (1, 't1', 'dark'), (2, 't2', NULL)",
    'INSERT INTO members (id, email, tenant_id, "managerId") VALUES'
    " ('al', 'al@acme.test', 't1', NULL),"
    " ('bo', 'bo@acme.test', 't1', 'al'),"
    " ('cy', 'cy@acme.test', 't1', 'al'),"
    " ('dee', 'dee@other.test', 't2', NULL)",
    "INSERT INTO groups (id, group_label) VALUES (1, 'admins'), (2, 'readers')",
    'INSERT INTO "_GroupToMember" ("A", "B") VALUES (1, \'al\'), (2, \'al\'), (2, \'bo\')',
    "INSERT INTO regions (tenant_id, code, label) VALUES ('t1', 'eu', 'Europe'), ('t1', 'us', 'America')",
    'INSERT INTO seats (id, region_tenant_id, region_code) VALUES'
    " ('s1', 't1', 'eu'), ('s2', 't1', 'eu'), ('s3', 't1', 'us')",
)


@pytest.fixture(name='session')
def session_fixture(models: Any) -> Iterator[orm.Session]:
    """A live database built from the emitted models, with rows in it."""
    if URL is None or shutil.which('pg_dump') is None:  # pragma: no cover - mirrored by `needs_database`
        pytest.skip('PRISMA_SA_TEST_DATABASE_URL is not set, or pg_dump is missing')

    base, _, database = URL.rstrip('/').rpartition('/')
    target = f'{database}_relmode_orm'
    recreate(URL, target)

    engine = sa.create_engine(sqlalchemy_url(f'{base}/{target}'))
    try:
        models.Base.metadata.create_all(engine)
        with orm.Session(engine) as session:
            for statement in ROWS:
                session.execute(sa.text(statement))
            session.commit()
        with orm.Session(engine) as session:
            yield session
    finally:
        engine.dispose()
        drop(URL, target)


@needs_database
def test_to_one_owner_loads(session: orm.Session, models: Any) -> None:
    member = session.get(models.Member, 'bo')
    assert member is not None
    assert member.tenant.name == 'Acme'


@needs_database
def test_to_many_loads_only_its_own_rows(session: orm.Session, models: Any) -> None:
    tenant = session.get(models.Tenant, 't1')
    assert tenant is not None
    assert sorted(member.id for member in tenant.members) == ['al', 'bo', 'cy']


@needs_database
def test_to_one_inverse_loads(session: orm.Session, models: Any) -> None:
    tenant = session.get(models.Tenant, 't1')
    assert tenant is not None
    assert tenant.config.theme == 'dark'
    assert tenant.config.tenant.id == 't1'


@needs_database
def test_self_relation_loads_in_both_directions(session: orm.Session, models: Any) -> None:
    """The direction is the part a compiling mapping still gets wrong."""
    al = session.get(models.Member, 'al')
    bo = session.get(models.Member, 'bo')
    assert al is not None and bo is not None

    assert sorted(report.id for report in al.reports) == ['bo', 'cy']
    assert al.manager is None
    assert bo.manager is not None and bo.manager.id == 'al'
    assert list(bo.reports) == []


@needs_database
def test_many_to_many_loads_from_both_sides(session: orm.Session, models: Any) -> None:
    """`A` is `Group` and `B` is `Member`; swapping them returns other rows."""
    al = session.get(models.Member, 'al')
    bo = session.get(models.Member, 'bo')
    admins = session.get(models.Group, 1)
    readers = session.get(models.Group, 2)
    assert al is not None and bo is not None and admins is not None and readers is not None

    assert sorted(group.label for group in al.groups) == ['admins', 'readers']
    assert sorted(group.label for group in bo.groups) == ['readers']
    assert sorted(member.id for member in admins.members) == ['al']
    assert sorted(member.id for member in readers.members) == ['al', 'bo']


@needs_database
def test_compound_foreign_key_relation_loads(session: orm.Session, models: Any) -> None:
    """Both halves of the conjunction matter: `eu` and `us` share a tenant."""
    seat = session.get(models.Seat, 's3')
    assert seat is not None
    assert seat.region.label == 'America'

    region = session.get(models.Region, ('t1', 'eu'))
    assert region is not None
    assert sorted(item.id for item in region.seats) == ['s1', 's2']


@needs_database
def test_a_join_through_the_relationship_compiles_and_runs(session: orm.Session, models: Any) -> None:
    """Not just lazy loads: the same condition has to work as an explicit join."""
    names = session.execute(
        sa.select(models.Tenant.name).join(models.Tenant.members).where(models.Member.id == 'dee')
    ).all()
    assert [row[0] for row in names] == ['Other']


@needs_database
def test_pg_dump_matches_the_core_metadata(models: Any, relation_mode_metadata: sa.MetaData) -> None:
    """Everything `CreateTable` does not cover, on two real databases."""
    assert URL is not None
    base, _, database = URL.rstrip('/').rpartition('/')

    dumps: List[Any] = []
    for name, source in (('core', relation_mode_metadata), ('declarative', models.Base.metadata)):
        target = f'{database}_relmode_{name}'
        recreate(URL, target)
        engine = sa.create_engine(sqlalchemy_url(f'{base}/{target}'))
        try:
            source.create_all(engine)
        finally:
            engine.dispose()
        dumps.append((target, dump(f'{base}/{target}')))

    (core_name, core_dump), (declarative_name, declarative_dump) = dumps
    for name in (core_name, declarative_name):
        drop(URL, name)

    assert core_dump == declarative_dump, 'declarative DDL differs from the Core metadata:\n' + '\n'.join(
        difflib.unified_diff(core_dump, declarative_dump, 'core', 'declarative', lineterm='')
    )
    assert sum(1 for line in core_dump if line.startswith(('CREATE', 'ALTER'))) > 20
