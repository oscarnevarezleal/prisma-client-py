"""The declarative emitter must not drift from the Core layer.

`test_ddl_equivalence.py` proves `build_metadata()` describes the database
`prisma db push` creates. These tests prove the emitted declarative source
describes *the same* metadata --- table by table as compiled DDL, and then as
`pg_dump` output from two real databases. That transitivity is the only thing
that makes a second representation of the schema safe to check in: on its own a
declarative layer that drifts is worse than none, because it looks
authoritative.
"""

from __future__ import annotations

import os
import sys
import copy
import shutil
import difflib
import subprocess
import importlib.util
from typing import Any, Dict, List, Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import orm
from sqlalchemy.schema import CreateIndex, CreateTable
from sqlalchemy.dialects import postgresql

from prisma.sa._build import build_metadata
from prisma.sa._types import UnsupportedProviderError
from prisma.sa._declarative import Refusal, UnsupportedShapeError, build_declarative

REPO_ROOT = Path(__file__).resolve().parents[2]
URL = os.environ.get('PRISMA_SA_TEST_DATABASE_URL')

# SQLAlchemy leaves `PGDialect.__init__` unannotated
DIALECT: sa.engine.Dialect = postgresql.dialect()  # type: ignore[no-untyped-call]

needs_database = pytest.mark.skipif(
    URL is None or shutil.which('pg_dump') is None,
    reason='PRISMA_SA_TEST_DATABASE_URL is not set, or pg_dump is missing',
)
needs_ruff = pytest.mark.skipif(shutil.which('ruff') is None, reason='ruff is not installed')
needs_pyright = pytest.mark.skipif(shutil.which('pyright') is None, reason='pyright is not installed')


# -- fixtures ----------------------------------------------------------------


@pytest.fixture(scope='module', name='emitted')
def emitted_fixture(generated: Dict[str, Any]) -> Any:
    return build_declarative(generated['schema'], generated['enums'], generated['provider'])


@pytest.fixture(scope='module', name='models')
def models_fixture(emitted: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Any]:
    """The emitted source, imported as a real module.

    Imported rather than `exec`d: declarative resolves `Mapped[...]` annotations
    against the *defining module*, so a namespace dict with no `__name__` fails
    on the first class.
    """
    yield import_source(emitted.source, tmp_path_factory.mktemp('declarative') / 'models.py', 'prisma_sa_models')


def import_source(source: str, path: Path, name: str) -> Any:
    path.write_text(source)
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f'cannot import {path}'
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def variant(generated: Dict[str, Any]) -> Dict[str, Any]:
    """A deep copy of the reference schema, for tests that mutate it."""
    return copy.deepcopy(generated)


def create_table_ddl(metadata: sa.MetaData, name: str) -> str:
    return str(CreateTable(metadata.tables[name]).compile(dialect=DIALECT))


def index_ddl(metadata: sa.MetaData, name: str) -> List[str]:
    return sorted(str(CreateIndex(index).compile(dialect=DIALECT)) for index in metadata.tables[name].indexes)


def relationships(model: Any) -> Dict[str, Any]:
    return {name: rel for name, rel in sa.inspect(model).relationships.items()}


# -- the gate: same DDL as the Core layer ------------------------------------


def test_same_tables_as_the_core_metadata(models: Any, metadata: sa.MetaData) -> None:
    assert sorted(models.Base.metadata.tables) == sorted(metadata.tables)
    # a comparison that passes on two empty metadatas would prove nothing
    assert len(metadata.tables) > 10


def test_create_table_ddl_is_identical(models: Any, metadata: sa.MetaData) -> None:
    """Every column, type, default, nullability and constraint, compiled."""
    names = sorted(metadata.tables)
    core = [create_table_ddl(metadata, name) for name in names]
    declarative = [create_table_ddl(models.Base.metadata, name) for name in names]

    # the diff is only built when the assertion is about to fail
    assert core == declarative, 'declarative DDL differs from the Core metadata:\n' + '\n'.join(
        difflib.unified_diff('\n'.join(core).splitlines(), '\n'.join(declarative).splitlines(), 'core', 'declarative')
    )


def test_create_index_ddl_is_identical(models: Any, metadata: sa.MetaData) -> None:
    """Index names, uniqueness and per-column sort order."""
    names = sorted(metadata.tables)
    assert [index_ddl(models.Base.metadata, name) for name in names] == [index_ddl(metadata, name) for name in names]


def test_descending_index_member_survives(models: Any) -> None:
    """`@@index([scopeId, createdAt(sort: Desc)])`.

    Dropping the direction is not cosmetic on a composite index, and it is the
    one index detail a string-based `__table_args__` form cannot carry.
    """
    (ddl,) = [text for text in index_ddl(models.Base.metadata, 'regions')]
    assert ddl.endswith('(scope_id, created_at DESC)')


def test_sequences_are_declared(models: Any, metadata: sa.MetaData) -> None:
    """A non-primary-key `@default(autoincrement())` needs an explicit sequence."""
    for source in (metadata, models.Base.metadata):
        column = source.tables['Ticket'].c['ticketNumber']
        assert isinstance(column.default, sa.Sequence)
        assert column.default.name == 'Ticket_ticketNumber_seq'
        assert isinstance(column.default.data_type, sa.Integer)


def test_sequence_ownership_is_emitted(emitted: Any) -> None:
    """`ALTER SEQUENCE ... OWNED BY ...`, which SQLAlchemy has no API for."""
    assert 'ALTER SEQUENCE "Ticket_ticketNumber_seq" OWNED BY "Ticket"."ticketNumber"' in emitted.source
    assert 'ALTER SEQUENCE "Ticket_bigNumber_seq" OWNED BY "Ticket"."bigNumber"' in emitted.source


def test_enum_type_is_shared_not_duplicated(models: Any) -> None:
    """Two ENUM instances with the same name emit `CREATE TYPE` twice."""
    role = models.Base.metadata.tables['accounts'].c['role'].type
    assert isinstance(role, postgresql.ENUM)
    assert list(role.enums) == ['OWNER', 'administrator', 'VIEWER']
    instances = {
        id(column.type)
        for table in models.Base.metadata.tables.values()
        for column in table.c
        if isinstance(column.type, postgresql.ENUM)
    }
    assert len(instances) == 1


def test_output_is_deterministic(generated: Dict[str, Any], emitted: Any) -> None:
    """Regenerating an unchanged schema must produce an unchanged file."""
    again = build_declarative(generated['schema'], generated['enums'], generated['provider'])
    assert again.source == emitted.source


def test_unverified_provider_refuses(generated: Dict[str, Any]) -> None:
    with pytest.raises(UnsupportedProviderError):
        build_declarative(generated['schema'], generated['enums'], 'mysql')


# -- the ORM layer -----------------------------------------------------------


def test_mappers_configure(models: Any) -> None:
    """Every `back_populates`, `foreign_keys` and `remote_side` resolves."""
    orm.configure_mappers()
    assert sa.inspect(models.Account).local_table.name == 'accounts'


def test_attribute_names_are_prisma_field_names(models: Any) -> None:
    """`@map` moves the column, not the attribute. Call sites read the same."""
    assert models.Account.slug.expression.name == 'url_slug'
    assert models.Region.createdAt.expression.name == 'created_at'
    assert models.Label.name.expression.name == 'label_name'
    assert not hasattr(models.Account, 'url_slug')


def test_table_names_are_mapped_names(models: Any) -> None:
    assert models.Account.__tablename__ == 'accounts'  # @@map("accounts")
    assert models.Entry.__tablename__ == 'Entry'  # unmapped


def test_to_one_owner_relation(models: Any) -> None:
    account = relationships(models.Entry)['account']
    assert account.direction is orm.MANYTOONE
    assert account.back_populates == 'posts'
    assert not account.uselist


def test_to_many_relation(models: Any) -> None:
    posts = relationships(models.Account)['posts']
    assert posts.direction is orm.ONETOMANY
    assert posts.back_populates == 'account'
    assert posts.uselist


def test_to_one_inverse_relation(models: Any) -> None:
    """The FK is on the other side, but the attribute is still a single object."""
    profile = relationships(models.Account)['profile']
    assert profile.direction is orm.ONETOMANY  # the FK really is on Profile
    assert profile.uselist is False
    assert profile.back_populates == 'account'


def test_self_referential_to_one_uses_remote_side(models: Any) -> None:
    """Without `remote_side` SQLAlchemy cannot tell which end is the parent."""
    parent = relationships(models.Entry)['parent']
    assert parent.direction is orm.MANYTOONE
    assert [column.name for column in parent.remote_side] == ['id']
    assert relationships(models.Entry)['children'].direction is orm.ONETOMANY


def test_many_to_many_relation(models: Any) -> None:
    labels = relationships(models.Entry)['labels']
    assert labels.direction is orm.MANYTOMANY
    assert labels.secondary is not None and labels.secondary.name == '_EntryToLabel'
    assert relationships(models.Label)['entries'].back_populates == 'labels'


def test_back_populates_is_wired_on_both_sides(models: Any) -> None:
    for model, field in (
        (models.Account, 'posts'),
        (models.Account, 'profile'),
        (models.Entry, 'account'),
        (models.Entry, 'parent'),
        (models.Entry, 'children'),
        (models.Entry, 'labels'),
        (models.Profile, 'account'),
        (models.Label, 'entries'),
        (models.Ticket, 'links'),
        (models.Region, 'ticket'),
    ):
        relation = relationships(model)[field]
        other = relation.mapper.class_
        assert relation.back_populates is not None, f'{model.__name__}.{field}'
        assert relationships(other)[relation.back_populates].back_populates == field


def test_compound_primary_key(models: Any) -> None:
    assert [column.name for column in sa.inspect(models.Composite).primary_key] == ['left', 'right']


def test_identifiers_that_overflow_are_truncated_like_prisma(models: Any) -> None:
    """A derived name over 63 characters is not a diff, it is a hard failure.

    PostgreSQL truncates at 63 and so does Prisma; SQLAlchemy raises
    `IdentifierError` before emitting any SQL. The reference schema overflows on
    both a compound `@@unique` and a foreign key, and both have to arrive here
    already truncated --- which they do, because the names come off the metadata
    `build_metadata()` built rather than being derived a second time.
    """
    tables = models.Base.metadata.tables.values()
    index_names = [str(index.name) for table in tables for index in table.indexes]
    key_names = [
        str(constraint.name)
        for table in tables
        for constraint in table.constraints
        if isinstance(constraint, sa.ForeignKeyConstraint)
    ]

    assert max(len(name) for name in index_names) == 63
    assert max(len(name) for name in key_names) == 63


def test_native_types_reach_the_annotations(models: Any) -> None:
    """`@db.Uuid` comes back as a `uuid.UUID`, and the annotation says so."""
    ticket = models.Base.metadata.tables['Ticket']
    assert str(ticket.c['reference'].type.compile(dialect=DIALECT)) == 'VARCHAR(40)'
    assert str(ticket.c['seenAt'].type.compile(dialect=DIALECT)) == 'TIMESTAMP(6) WITH TIME ZONE'
    assert models.Ticket.__annotations__['id'] == 'Mapped[uuid.UUID]'


def test_array_annotation_is_a_list(models: Any) -> None:
    assert models.Account.__annotations__['tags'] == 'Mapped[Optional[List[str]]]'


def test_enum_class_carries_the_stored_labels(models: Any) -> None:
    """`@map` on an enum value: the Python name is not the stored label."""
    assert models.Role.ADMIN.value == 'administrator'
    assert models.Role.ADMIN == 'administrator'  # a `str` enum, so it assigns straight through
    assert models.Account.__annotations__['role'] == 'Mapped[str]'


# -- refusals ----------------------------------------------------------------


def test_refuses_self_referential_many_to_many(models: Any, emitted: Any) -> None:
    """The DMMF does not say which join column each side traverses.

    Guessing reverses the relation and silently returns the wrong rows, so
    nothing is emitted and the attribute is simply absent --- a call site that
    used it fails loudly instead.
    """
    refused = {refusal.field for refusal in emitted.refusals}
    assert refused == {'related', 'similar'}
    assert not hasattr(models.Label, 'related')
    assert not hasattr(models.Label, 'similar')

    # the join table is unambiguous even though the direction is not, so it is
    # still in the metadata and the DDL is unchanged
    assert '_similar' in models.Base.metadata.tables
    assert '`related` is not emitted' in emitted.source


def test_refusals_name_the_shape(emitted: Any) -> None:
    # asserted, not assumed: an empty tuple makes the loop below vacuous
    assert len(emitted.refusals) == 2
    for refusal in emitted.refusals:
        assert isinstance(refusal, Refusal)
        assert refusal.shape == 'many-to-many'
        assert refusal.kind == 'relation'
        assert refusal.model == 'Label'
        assert 'Label.' in refusal.describe()


def test_strict_raises_instead_of_skipping(generated: Dict[str, Any]) -> None:
    with pytest.raises(UnsupportedShapeError) as exc:
        build_declarative(generated['schema'], generated['enums'], generated['provider'], strict=True)

    assert 'Label.related' in str(exc.value)
    assert len(exc.value.refusals) == 2


def test_model_without_a_primary_key_becomes_a_core_table(generated: Dict[str, Any]) -> None:
    """Prisma allows it; the SQLAlchemy ORM cannot map a class without one.

    Inventing a key would change which rows the identity map treats as the same
    row, so the table is emitted as Core and the DDL is untouched.
    """
    schema = variant(generated)['schema']
    schema['Composite']['primary_key'] = {'name': None, 'db_name': None, 'fields': [], 'columns': []}

    emitted = build_declarative(schema, generated['enums'], generated['provider'])
    (refusal,) = [item for item in emitted.refusals if item.model == 'Composite']
    assert refusal.kind == 'model'
    assert 'no primary key' in refusal.reason
    assert 'class Composite(Base)' not in emitted.source
    assert "t_Composite = sa.Table(\n    'Composite'," in emitted.source


def test_relation_to_an_unmappable_model_is_refused(generated: Dict[str, Any]) -> None:
    copied = variant(generated)
    copied['schema']['Profile']['primary_key'] = {'name': None, 'db_name': None, 'fields': [], 'columns': []}

    emitted = build_declarative(copied['schema'], copied['enums'], copied['provider'])
    (refusal,) = [item for item in emitted.refusals if item.field == 'profile']
    assert 'Profile could not be mapped' in refusal.reason


def test_reserved_attribute_name_is_refused(generated: Dict[str, Any]) -> None:
    """A Prisma field called `metadata` shadows `Base.metadata`.

    Left alone it fails at import with an error naming neither the model nor the
    field; renaming it would silently change every call site.
    """
    copied = variant(generated)
    fields = copied['schema']['Composite']['fields']
    fields['metadata'] = fields.pop('note')

    emitted = build_declarative(copied['schema'], copied['enums'], copied['provider'])
    (refusal,) = [item for item in emitted.refusals if item.model == 'Composite']
    assert 'metadata' in refusal.reason
    assert 'class Composite(Base)' not in emitted.source


def test_the_other_side_of_a_refused_relation_goes_with_it(generated: Dict[str, Any]) -> None:
    """`back_populates` has to name an attribute that exists.

    A one-sided refusal emits a mapping that fails at `configure_mappers()`,
    which is a stack trace pointing at SQLAlchemy rather than at the schema.
    """
    copied = variant(generated)
    copied['schema']['Entry']['relations']['labels']['join_ambiguous'] = True

    emitted = build_declarative(copied['schema'], copied['enums'], copied['provider'])
    reasons = {(item.model, item.field): item.reason for item in emitted.refusals}
    assert 'not recoverable' in reasons[('Entry', 'labels')]
    assert 'back_populates' in reasons[('Label', 'entries')]


def test_named_primary_key_constraint(generated: Dict[str, Any], tmp_path: Path) -> None:
    """`@@id(map: "...")` is the only case that needs an explicit constraint."""
    copied = variant(generated)
    copied['schema']['Composite']['primary_key']['db_name'] = 'composite_pk'

    emitted = build_declarative(copied['schema'], copied['enums'], copied['provider'])
    assert "sa.PrimaryKeyConstraint('left', 'right', name='composite_pk')" in emitted.source

    core = build_metadata(copied['schema'], copied['enums'], copied['provider'])
    module = import_source(emitted.source, tmp_path / 'named_pk.py', 'prisma_sa_named_pk')
    assert create_table_ddl(module.Base.metadata, 'Composite') == create_table_ddl(core, 'Composite')


# -- the emitted file has to survive the tools it will be checked with -------


MINIMAL_SCHEMA = {
    'Thing': {
        'table': 'Thing',
        'primary_key': {'name': None, 'db_name': None, 'fields': ['id'], 'columns': ['id']},
        'fields': {
            'id': {
                'column': 'id',
                'kind': 'scalar',
                'type': 'String',
                'is_list': False,
                'nullable': False,
                'is_id': True,
                'is_unique': False,
                'is_read_only': False,
                'is_updated_at': False,
                'default': None,
                'native_type': None,
                'sequence': None,
            }
        },
        'relations': {},
        'uniques': [],
        'indexes': [],
    }
}


@needs_ruff
def test_a_schema_that_needs_no_imports_still_emits_a_clean_file(tmp_path: Path) -> None:
    """No enums, no arrays, no sequences, no `Optional` --- so no imports.

    The import block is assembled from what the body turned out to need, and an
    empty one is the case where a stray blank line survives into the output and
    `ruff format` rewrites the file on first contact.
    """
    emitted = build_declarative(MINIMAL_SCHEMA, {}, 'postgresql')
    assert 'postgresql' not in emitted.source
    assert not emitted.refusals

    path = tmp_path / 'minimal.py'
    path.write_text(emitted.source)
    config = str(REPO_ROOT / 'pyproject.toml')
    for command in (['format', '--check'], ['check']):
        result = subprocess.run(['ruff', *command, '--config', config, str(path)], capture_output=True, text=True)
        assert result.returncode == 0, f'ruff {command[0]}:\n{result.stdout}\n{result.stderr}'

    module = import_source(emitted.source, tmp_path / 'minimal_models.py', 'prisma_sa_minimal')
    assert create_table_ddl(module.Base.metadata, 'Thing') == create_table_ddl(
        build_metadata(MINIMAL_SCHEMA, {}, 'postgresql'), 'Thing'
    )


@needs_ruff
def test_source_is_formatted(emitted: Any, tmp_path: Path) -> None:
    path = tmp_path / 'models.py'
    path.write_text(emitted.source)
    config = str(REPO_ROOT / 'pyproject.toml')
    for command in (['format', '--check'], ['check']):
        result = subprocess.run(
            ['ruff', *command, '--config', config, str(path)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f'ruff {command[0]}:\n{result.stdout}\n{result.stderr}'


def test_type_checks_with_mypy(emitted: Any, tmp_path: Path) -> None:
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


# -- the strongest check: two real databases ---------------------------------


@needs_database
def test_pg_dump_is_identical(models: Any, metadata: sa.MetaData) -> None:
    """Everything `CreateTable` does not cover: types, sequences, ownership.

    Against the Core metadata rather than against `prisma db push` --- the two
    are already proven identical by `test_ddl_equivalence.py`, and re-running
    the CLI here would double a slow fixture to prove the same transitivity.
    """
    assert URL is not None
    base, _, database = URL.rstrip('/').rpartition('/')

    dumps = []
    for name, source in (('core', metadata), ('declarative', models.Base.metadata)):
        target = f'{database}_prisma_{name}'
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


def sqlalchemy_url(url: str) -> str:
    _, _, rest = url.partition('://')
    return f'postgresql+psycopg://{rest}'


def recreate(admin_url: str, name: str) -> None:
    engine = sa.create_engine(sqlalchemy_url(admin_url), isolation_level='AUTOCOMMIT')
    with engine.connect() as conn:
        conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}"'))
        conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    engine.dispose()


def drop(admin_url: str, name: str) -> None:
    engine = sa.create_engine(sqlalchemy_url(admin_url), isolation_level='AUTOCOMMIT')
    with engine.connect() as conn:
        conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}"'))
    engine.dispose()


def dump(url: str) -> List[str]:
    out = subprocess.run(
        ['pg_dump', '--schema-only', '--no-owner', '--no-acl', '--no-comments', url],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    _, _, database = url.rstrip('/').rpartition('/')
    return [
        line.replace(database, '<db>')
        for line in out.splitlines()
        if line.strip() and not line.startswith(('--', '\\restrict', '\\unrestrict'))
    ]
