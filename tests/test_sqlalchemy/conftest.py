from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Iterator

import pytest

from ..dmmf_sample import SAMPLE, RELATION_MODE_SAMPLE, schema_text, loaded_datamodel

pytest.importorskip('sqlalchemy', reason='prisma[sqlalchemy] is not installed')

if TYPE_CHECKING:
    import sqlalchemy as sa


@pytest.fixture(scope='package', name='generated')
def generated_fixture() -> Iterator[Dict[str, Any]]:
    """`SCHEMA` / `ENUM_SCHEMA` as `schemaMetadata = true` would emit them."""
    if not SAMPLE.exists():  # pragma: no cover
        pytest.skip(f'wire sample not recorded at {SAMPLE}')

    from prisma.generator.models import build_enum_metadata, build_schema_metadata

    with loaded_datamodel() as datamodel:
        yield {
            'schema': build_schema_metadata(datamodel, schema_text()),
            'enums': build_enum_metadata(datamodel),
            'provider': 'postgresql',
        }


@pytest.fixture(scope='package', name='relation_mode_generated')
def relation_mode_generated_fixture() -> Iterator[Dict[str, Any]]:
    """The same, for the second sample, whose datasource is `relationMode = "prisma"`.

    A separate schema rather than a separate model in the first one: the setting
    is on the datasource, and a schema has exactly one of those.
    """
    if not RELATION_MODE_SAMPLE.exists():  # pragma: no cover
        pytest.skip(f'wire sample not recorded at {RELATION_MODE_SAMPLE}')

    from prisma.generator.models import build_enum_metadata, build_schema_metadata

    with loaded_datamodel(RELATION_MODE_SAMPLE) as datamodel:
        yield {
            'schema': build_schema_metadata(datamodel, schema_text(RELATION_MODE_SAMPLE)),
            'enums': build_enum_metadata(datamodel),
            'provider': 'postgresql',
        }


@pytest.fixture(scope='package', name='relation_mode_metadata')
def relation_mode_metadata_fixture(relation_mode_generated: Dict[str, Any]) -> 'sa.MetaData':
    from prisma.sa import build_metadata

    return build_metadata(
        relation_mode_generated['schema'],
        relation_mode_generated['enums'],
        relation_mode_generated['provider'],
    )


@pytest.fixture(scope='package', name='metadata')
def metadata_fixture(generated: Dict[str, Any]) -> 'sa.MetaData':
    from prisma.sa import build_metadata

    return build_metadata(generated['schema'], generated['enums'], generated['provider'])


@pytest.fixture(name='installed')
def installed_fixture(generated: Dict[str, Any]) -> Iterator[None]:
    """Make `prisma.sa`'s public API behave as if the client were generated
    with `schemaMetadata = true`.

    The dev client is generated without it — deliberately, since that is the
    default — so `sa.metadata()` would otherwise raise. Everything the public
    accessors read goes through `prisma.metadata`, so populating that module is
    the whole of the setup.
    """
    import prisma.metadata
    from prisma import sa as prisma_sa

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(prisma.metadata, 'SCHEMA', generated['schema'], raising=False)
        patch.setattr(prisma.metadata, 'ENUM_SCHEMA', generated['enums'], raising=False)
        patch.setattr(prisma.metadata, 'DATABASE_PROVIDER', generated['provider'], raising=False)
        prisma_sa.clear_cache()
        try:
            yield
        finally:
            # the cache holds a MetaData built from the patched module; leaving
            # it in place would leak into tests that expect the real one
            prisma_sa.clear_cache()
