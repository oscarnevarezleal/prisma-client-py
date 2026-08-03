"""Loading the recorded DMMF wire sample.

Shared by the generator tests and the SQLAlchemy tests so there is one place
that knows how the sample is shaped and what context the generator models need
in order to validate.

The sample is recorded from a real `prisma generate` against
`tests/test_generation/data/dmmf_wire_sample.prisma`. To re-record::

    cd <a scratch dir with that schema>
    PRISMA_PY_DEBUG_GENERATOR=1 python -m prisma generate --schema=schema.prisma

then copy `src/prisma/generator/debug-params.json`, keeping only `datasources`
`datamodel` and `dmmf.datamodel` — `dmmf.schema` is ~420 KB of GraphQL input
types that nothing here reads. `datamodel` is the raw schema text and must be
kept: it is the only source of `@db.*` native types.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Iterator, cast
from pathlib import Path
from contextlib import contextmanager

from prisma._compat import model_parse_strict
from prisma.generator.models import Config, Datamodel, data_ctx

SAMPLE = Path(__file__).parent / 'test_generation' / 'data' / 'dmmf_wire_sample.json'


class _FakeDatasource:
    active_provider = 'postgresql'


class _FakeDmmf:
    def __init__(self, datamodel: Datamodel) -> None:
        self.datamodel = datamodel


class _FakeData:
    """The slice of `GenericData` the derivation reaches for.

    The recorded sample is trimmed, so it cannot be parsed as a full
    `PythonData`; `get_datamodel()` and `sql_param()` only ever touch these two
    attributes.
    """

    def __init__(self, datamodel: Datamodel) -> None:
        self.dmmf = _FakeDmmf(datamodel)
        self.datasources: List[Any] = [_FakeDatasource()]


def read_wire_sample() -> Dict[str, Any]:
    return cast('Dict[str, Any]', json.loads(SAMPLE.read_text()))


def schema_text() -> str:
    """The raw `schema.prisma` contents as Prisma sent them.

    Needed for `@db.*` native types, which Prisma does not put in the DMMF at
    all — they are only recoverable by lexing this.
    """
    return str(read_wire_sample()['datamodel'])


@contextmanager
def loaded_datamodel() -> Iterator[Datamodel]:
    """Parse the sample and publish it to the generator's context vars."""
    # `Decimal` fields refuse to validate without the experimental flag, and
    # constructing a Config is what publishes it to the context.
    Config(enable_experimental_decimal=True)

    datamodel = model_parse_strict(Datamodel, read_wire_sample()['dmmf']['datamodel'])

    # `_FakeData` is deliberately only the slice of `GenericData` the derivation
    # reads (see its docstring), so it cannot satisfy the context var's type.
    token = data_ctx.set(cast(Any, _FakeData(datamodel)))
    try:
        yield datamodel
    finally:
        data_ctx.reset(token)
