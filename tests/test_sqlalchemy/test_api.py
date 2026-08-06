"""The public surface of `prisma.sa`.

`test_build.py` covers what `build_metadata()` produces. This covers how you
reach it: the accessors, their failure modes, the cache, and the promise that
importing Prisma does not drag SQLAlchemy in with it.
"""

from __future__ import annotations

import sys
import subprocess

import pytest
import sqlalchemy as sa

from prisma import sa as prisma_sa
from prisma._schema import SchemaNotAvailableError


@pytest.mark.usefixtures('installed')
def test_metadata_returns_a_metadata() -> None:
    md = prisma_sa.metadata()
    assert isinstance(md, sa.MetaData)
    assert 'accounts' in md.tables


@pytest.mark.usefixtures('installed')
def test_metadata_is_cached() -> None:
    """Callers pass this to Alembic and to query builders.

    Rebuilding per call would hand out `Table` objects that compare unequal to
    each other, and SQLAlchemy would reject a query mixing them.
    """
    assert prisma_sa.metadata() is prisma_sa.metadata()


@pytest.mark.usefixtures('installed')
def test_clear_cache_rebuilds() -> None:
    first = prisma_sa.metadata()
    prisma_sa.clear_cache()
    assert prisma_sa.metadata() is not first


@pytest.mark.usefixtures('installed')
def test_table_for_takes_the_model_name() -> None:
    """`@@map` is the entire point — `table_for` resolves it."""
    assert prisma_sa.table_for('Account').name == 'accounts'
    assert prisma_sa.table_for('Entry').name == 'Entry'


@pytest.mark.usefixtures('installed')
def test_table_for_rejects_a_table_name() -> None:
    """Passing 'accounts' instead of 'Account' is the obvious mistake."""
    with pytest.raises(LookupError, match='Unknown model: accounts'):
        prisma_sa.table_for('accounts')


@pytest.mark.usefixtures('installed')
def test_join_table_for() -> None:
    """The join table has no model, so `table_for` cannot reach it."""
    join = prisma_sa.join_table_for('Entry', 'labels')
    assert join.name == '_EntryToLabel'
    # both sides name the same table
    assert prisma_sa.join_table_for('Label', 'entries') is join


@pytest.mark.usefixtures('installed')
def test_join_table_for_rejects_other_shapes() -> None:
    """Only an implicit m2m has one; the error should say which shape it got."""
    with pytest.raises(LookupError, match='to-many relation'):
        prisma_sa.join_table_for('Account', 'posts')

    with pytest.raises(LookupError, match='to-one-owner relation'):
        prisma_sa.join_table_for('Entry', 'account')


@pytest.mark.usefixtures('installed')
def test_join_table_for_unknown_relation() -> None:
    with pytest.raises(LookupError, match='Unknown relation: Account.nope'):
        prisma_sa.join_table_for('Account', 'nope')


def test_accessors_explain_the_missing_option() -> None:
    """Without `schemaMetadata = true` there is nothing to build from.

    The error has to name the option; nothing else in the client mentions it,
    so a bare KeyError would leave the user with no way forward.
    """
    prisma_sa.clear_cache()
    with pytest.raises(SchemaNotAvailableError) as exc:
        prisma_sa.metadata()

    assert 'schemaMetadata = true' in str(exc.value)
    assert 'prisma generate' in str(exc.value)


def test_importing_prisma_does_not_import_sqlalchemy() -> None:
    """SQLAlchemy costs ~157ms and ~26MB to import.

    Most users are not using it, and `prisma.sa` is reached through a module
    `__getattr__` specifically so they do not pay for it. Run in a subprocess
    because this test session has SQLAlchemy imported already.
    """
    result = subprocess.run(
        [sys.executable, '-c', 'import prisma, sys; print("sqlalchemy" in sys.modules)'],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == 'False', result.stderr


def test_public_names_all_resolve() -> None:
    """`__all__` includes names served by a module `__getattr__`.

    A typo there is invisible until someone imports the name.
    """
    for name in prisma_sa.__all__:
        assert getattr(prisma_sa, name) is not None, name


def test_unknown_attribute_still_raises() -> None:
    """The `__getattr__` must not swallow real typos."""
    with pytest.raises(AttributeError, match='has no attribute'):
        prisma_sa.definitely_not_a_thing  # noqa: B018
