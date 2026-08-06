"""Tests for `prisma._schema`, the runtime view of the physical database schema.

The dev client is generated without `schemaMetadata`, so the accessors are
exercised against a hand-written metadata module rather than the real one — the
point here is the accessor behaviour and the error messages, not the
reconstruction, which `tests/test_generation/test_schema_metadata.py` covers.
"""

from __future__ import annotations

from typing import Any, Dict, Iterator

import pytest

import prisma.metadata
from prisma._schema import (
    SchemaNotAvailableError,
    relation,
    enum_label,
    get_schema,
    column_name,
    get_provider,
    is_available,
    model_schema,
    get_enum_schema,
    primary_key_columns,
)

SCHEMA: Dict[str, Any] = {
    'Account': {
        'table': 'accounts',
        'primary_key': {'name': None, 'db_name': None, 'fields': ['id'], 'columns': ['id']},
        'fields': {
            'id': {'column': 'id'},
            'slug': {'column': 'url_slug'},
        },
        'relations': {
            'posts': {'to': 'Entry', 'shape': 'to-many', 'fk_required': True},
        },
        'uniques': [],
        'indexes': [],
    },
    'Keyless': {
        'table': 'keyless',
        'primary_key': {'name': None, 'db_name': None, 'fields': [], 'columns': []},
        'fields': {},
        'relations': {},
        'uniques': [{'name': 'email', 'db_name': 'keyless_email_key', 'fields': ['email'], 'columns': ['email']}],
        'indexes': [],
    },
}

ENUM_SCHEMA: Dict[str, Any] = {
    'Role': {'db_name': 'Role', 'values': {'ADMIN': 'administrator', 'VIEWER': 'VIEWER'}},
}


@pytest.fixture(name='generated')
def generated_fixture(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(prisma.metadata, 'SCHEMA', SCHEMA, raising=False)
    monkeypatch.setattr(prisma.metadata, 'ENUM_SCHEMA', ENUM_SCHEMA, raising=False)
    monkeypatch.setattr(prisma.metadata, 'DATABASE_PROVIDER', 'postgresql', raising=False)
    yield


def test_not_available_without_the_option() -> None:
    """The dev client is generated without `schemaMetadata`."""
    assert is_available() is False

    with pytest.raises(SchemaNotAvailableError) as exc:
        get_schema()

    # the error has to say how to fix it; nothing else in the client mentions
    # this option
    assert 'schemaMetadata = true' in str(exc.value)
    assert 'prisma generate' in str(exc.value)


@pytest.mark.parametrize('accessor', [get_schema, get_enum_schema, get_provider])
def test_every_accessor_reports_the_same_way(accessor: Any) -> None:
    with pytest.raises(SchemaNotAvailableError):
        accessor()


@pytest.mark.usefixtures('generated')
def test_available_when_generated() -> None:
    assert is_available() is True
    assert get_provider() == 'postgresql'
    assert model_schema('Account')['table'] == 'accounts'


@pytest.mark.usefixtures('generated')
def test_column_name_honours_map() -> None:
    assert column_name('Account', 'slug') == 'url_slug'
    assert column_name('Account', 'id') == 'id'


@pytest.mark.usefixtures('generated')
def test_column_name_on_a_relation_says_so() -> None:
    """`Account.posts` has no column; the naive answer is a confusing KeyError."""
    with pytest.raises(LookupError, match='is a relation, not a column'):
        column_name('Account', 'posts')


@pytest.mark.usefixtures('generated')
def test_unknown_names_are_named_in_the_error() -> None:
    with pytest.raises(LookupError, match='Unknown model: Nope'):
        model_schema('Nope')

    with pytest.raises(LookupError, match='Unknown field: Account.nope'):
        column_name('Account', 'nope')

    with pytest.raises(LookupError, match='Unknown relation: Account.nope'):
        relation('Account', 'nope')

    with pytest.raises(LookupError, match=r'Unknown enum member: Role\.NOPE'):
        enum_label('Role', 'NOPE')


@pytest.mark.usefixtures('generated')
def test_enum_label() -> None:
    assert enum_label('Role', 'ADMIN') == 'administrator'
    assert enum_label('Role', 'VIEWER') == 'VIEWER'


@pytest.mark.usefixtures('generated')
def test_relation_metadata() -> None:
    assert relation('Account', 'posts')['fk_required'] is True


@pytest.mark.usefixtures('generated')
def test_primary_key_may_be_empty() -> None:
    """Prisma permits a model identified only by a unique constraint.

    Callers that need a row identity have to fall back to `uniques` rather than
    assume a primary key exists.
    """
    assert primary_key_columns('Account') == ['id']
    assert primary_key_columns('Keyless') == []
    assert model_schema('Keyless')['uniques'][0]['columns'] == ['email']
