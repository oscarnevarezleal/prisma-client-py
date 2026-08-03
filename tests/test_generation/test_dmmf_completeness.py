"""Guard against silently dropping data Prisma sends us.

The DMMF models use pydantic's default `extra='ignore'`, so any key Prisma adds
to the wire payload — or any key we simply never modelled — vanishes without a
word. That is exactly how `@map` (`Field.dbName`), `@db.*`
(`Field.nativeType`), `@@index` (`Datamodel.indexes`) and multi-schema
(`Datasource.schemas`) came to be invisible to the generator despite being sent
on every single generation.

These tests assert the modelled field set covers the wire key set. A failure
means either Prisma started sending something new, or a model is missing a
field — both are things a human should look at, not discover months later as a
mysterious schema diff.
"""

from __future__ import annotations

import json
from typing import Any, Set, Dict, List, cast
from pathlib import Path

import pytest

from prisma._compat import model_fields
from prisma.generator.models import (
    Enum,
    Field,
    Index,
    Model,
    Datamodel,
    Datasource,
    IndexField,
    PrimaryKey,
    UniqueIndex,
)

# A recorded `params.json` from a real `prisma generate` against a 41-model
# PostgreSQL schema. Regenerate with PRISMA_PY_DEBUG_GENERATOR=1.
FIXTURE = Path(__file__).parent / 'data' / 'dmmf_wire_sample.json'


def aliases(model: type) -> Set[str]:
    """Every wire key the model accepts (alias if set, else the field name)."""
    out: Set[str] = set()
    for name, info in model_fields(model).items():
        alias = getattr(info, 'alias', None)
        out.add(alias or name)
    return out


def wire_keys(objects: List[Dict[str, Any]]) -> Set[str]:
    return {key for obj in objects for key in obj}


@pytest.fixture(scope='module', name='wire')
def wire_fixture() -> Dict[str, Any]:
    if not FIXTURE.exists():
        pytest.skip(f'wire sample not recorded at {FIXTURE}')
    return cast('Dict[str, Any]', json.loads(FIXTURE.read_text()))


def test_datamodel_covers_wire(wire: Dict[str, Any]) -> None:
    datamodel = wire['dmmf']['datamodel']
    missing = set(datamodel) - aliases(Datamodel)
    assert not missing, f'Datamodel drops wire keys: {sorted(missing)}'


def test_model_covers_wire(wire: Dict[str, Any]) -> None:
    models = wire['dmmf']['datamodel']['models']
    # `extension` is ours, not Prisma's; it never appears on the wire
    missing = wire_keys(models) - aliases(Model)
    assert not missing, f'Model drops wire keys: {sorted(missing)}'


def test_field_covers_wire(wire: Dict[str, Any]) -> None:
    fields = [f for model in wire['dmmf']['datamodel']['models'] for f in model['fields']]
    missing = wire_keys(fields) - aliases(Field)
    assert not missing, f'Field drops wire keys: {sorted(missing)}'


def test_index_covers_wire(wire: Dict[str, Any]) -> None:
    indexes = wire['dmmf']['datamodel'].get('indexes', [])
    if not indexes:
        pytest.skip('sample schema declares no indexes')
    missing = wire_keys(indexes) - aliases(Index)
    assert not missing, f'Index drops wire keys: {sorted(missing)}'

    index_fields = [f for index in indexes for f in index.get('fields', [])]
    missing = wire_keys(index_fields) - aliases(IndexField)
    assert not missing, f'IndexField drops wire keys: {sorted(missing)}'


def test_datasource_covers_wire(wire: Dict[str, Any]) -> None:
    missing = wire_keys(wire['datasources']) - aliases(Datasource)
    assert not missing, f'Datasource drops wire keys: {sorted(missing)}'


def test_enum_covers_wire(wire: Dict[str, Any]) -> None:
    enums = wire['dmmf']['datamodel'].get('enums', [])
    if not enums:
        pytest.skip('sample schema declares no enums')
    missing = wire_keys(enums) - aliases(Enum)
    assert not missing, f'Enum drops wire keys: {sorted(missing)}'


def test_constraint_models_cover_wire(wire: Dict[str, Any]) -> None:
    models = wire['dmmf']['datamodel']['models']

    pks = [m['primaryKey'] for m in models if m.get('primaryKey')]
    if pks:
        missing = wire_keys(pks) - aliases(PrimaryKey)
        assert not missing, f'PrimaryKey drops wire keys: {sorted(missing)}'

    uniques = [u for m in models for u in m.get('uniqueIndexes', [])]
    if uniques:
        missing = wire_keys(uniques) - aliases(UniqueIndex)
        assert not missing, f'UniqueIndex drops wire keys: {sorted(missing)}'


def test_indexes_are_actually_parsed(wire: Dict[str, Any]) -> None:
    """The regression this whole module exists for.

    `datamodel.indexes` was on the wire and discarded, which made every
    `@@index` invisible to the generator.
    """
    datamodel = Datamodel.model_validate(wire['dmmf']['datamodel'])
    raw = wire['dmmf']['datamodel'].get('indexes', [])
    assert len(datamodel.indexes) == len(raw)
    if raw:
        assert datamodel.indexes[0].model == raw[0]['model']
        assert [f.name for f in datamodel.indexes[0].fields] == [f['name'] for f in raw[0]['fields']]
