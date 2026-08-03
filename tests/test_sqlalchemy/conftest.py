from __future__ import annotations

from typing import Any, Dict, Iterator

import pytest

from ..dmmf_sample import SAMPLE, loaded_datamodel

sqlalchemy = pytest.importorskip('sqlalchemy', reason='prisma[sqlalchemy] is not installed')


@pytest.fixture(scope='package', name='generated')
def generated_fixture() -> Iterator[Dict[str, Any]]:
    """`SCHEMA` / `ENUM_SCHEMA` as `schemaMetadata = true` would emit them."""
    if not SAMPLE.exists():  # pragma: no cover
        pytest.skip(f'wire sample not recorded at {SAMPLE}')

    from prisma.generator.models import build_enum_metadata, build_schema_metadata

    with loaded_datamodel() as datamodel:
        yield {
            'schema': build_schema_metadata(datamodel),
            'enums': build_enum_metadata(datamodel),
            'provider': 'postgresql',
        }


@pytest.fixture(scope='package', name='metadata')
def metadata_fixture(generated: Dict[str, Any]) -> Any:
    from prisma.sa import build_metadata

    return build_metadata(generated['schema'], generated['enums'], generated['provider'])
