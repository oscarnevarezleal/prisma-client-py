"""msgspec-backed record models (``modelBackend = "msgspec"``).

`msgspec <https://jcristharif.com/msgspec/>`_ decodes engine responses into
typed structs entirely in C — measured 5-9x faster than both pydantic-core
validation and this fork's hand-rolled slim backend — and compiles no
per-model schemas, so imports stay flat regardless of schema size.

Pydantic retro-compatibility: generated records keep the pydantic-shaped
surface most integrations rely on —

- ``model_dump()`` / ``dict()`` — recursive, python-mode values
- ``model_dump_json()`` / ``json()`` — via ``msgspec.json.encode`` (fast path)
- keyword construction (``User(id=..., email=...)``) via msgspec's own
  ``__init__``
- ``Model.prisma()`` for model-scoped queries
- ``to_pydantic()`` — an actual ``pydantic.BaseModel`` instance for
  integrations that require one (e.g. FastAPI ``response_model``); the twin
  class is built lazily with ``pydantic.create_model`` and cached, so the
  pydantic cost is only paid where it is actually demanded.

Like the slim backend, structs convert *trusted engine data*; they are not a
validation layer for arbitrary input. ``msgspec`` is an optional dependency:
``pip install msgspec``.
"""

from __future__ import annotations

import json
import decimal
import datetime
import importlib
from typing import Any, Dict, List, Type, TypeVar, ClassVar, Optional

try:
    import msgspec
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        'modelBackend = "msgspec" requires the msgspec package to be installed.\n'
        'Install it with: pip install msgspec'
    ) from exc

_RecordT = TypeVar('_RecordT', bound='PrismaRecord')

_PYDANTIC_TWINS: Dict[type, Any] = {}


def _dec_hook(typ: type, obj: Any) -> Any:
    """Handle the two prisma types msgspec has no native decoding for."""
    from . import fields

    if typ is fields.Base64:
        return fields.Base64.fromb64(obj) if isinstance(obj, str) else obj
    if isinstance(typ, type) and typ.__name__ == 'Json':
        # engine serializes Json columns as JSON strings; the pydantic backend
        # stores the decoded python object, so match that behavior
        return json.loads(obj) if isinstance(obj, str) else obj
    raise NotImplementedError(f'msgspec backend cannot decode values of type {typ!r}')


def _enc_hook(obj: Any) -> Any:
    from . import fields

    if isinstance(obj, fields.Base64):
        return str(obj)
    if isinstance(obj, decimal.Decimal):
        return str(obj)
    raise NotImplementedError(f'msgspec backend cannot encode values of type {type(obj)!r}')


def parse(model: Type[_RecordT], data: Any) -> _RecordT:
    """Convert a trusted engine response into a record struct."""
    # strict=False enables the coercions the wire protocol needs:
    # ISO strings -> datetime, strings -> int (BigInt) / Decimal
    return msgspec.convert(data, type=model, strict=False, dec_hook=_dec_hook)


class PrismaRecord(msgspec.Struct, kw_only=True):
    """Base struct for generated records (``modelBackend = "msgspec"``)."""

    # routing marker for prisma._compat.model_parse
    __prisma_struct__: ClassVar[bool] = True
    # the query builder reads field specs the same way as the slim backend
    __prisma_slim__: ClassVar[bool] = True
    __prisma_fields__: ClassVar[Dict[str, Any]] = {}

    @classmethod
    def from_engine(cls: Type[_RecordT], data: Dict[str, Any]) -> _RecordT:
        return parse(cls, data)

    @classmethod
    def prisma(cls: Type[_RecordT], client: Any = None) -> Any:
        """Model-scoped query access, mirroring the pydantic backend."""
        package = cls.__module__.rsplit('.', 1)[0]
        actions = importlib.import_module(f'{package}.actions')
        if client is None:
            client = importlib.import_module(f'{package}.client').get_client()
        return getattr(actions, f'{cls.__name__}Actions')(client, cls)

    # -- pydantic-shaped surface ------------------------------------------ #
    def model_dump(self, *, exclude_none: bool = False) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for name in self.__struct_fields__:
            value = getattr(self, name, None)
            if value is None and exclude_none:
                continue
            if isinstance(value, PrismaRecord):
                value = value.model_dump(exclude_none=exclude_none)
            elif isinstance(value, list):
                value = [
                    item.model_dump(exclude_none=exclude_none) if isinstance(item, PrismaRecord) else item
                    for item in value
                ]
            out[name] = value
        return out

    def dict(self, *, exclude_none: bool = False) -> Dict[str, Any]:
        return self.model_dump(exclude_none=exclude_none)

    def model_dump_json(self) -> str:
        return msgspec.json.encode(self, enc_hook=_enc_hook).decode()

    def json(self) -> str:
        return self.model_dump_json()

    def to_pydantic(self) -> Any:
        """An equivalent ``pydantic.BaseModel`` instance, for integrations
        that require real pydantic objects. The twin model class is created
        lazily and cached; relation fields are typed ``Any`` and carried as
        nested twins."""
        twin = _pydantic_twin(type(self))
        values: Dict[str, Any] = {}
        for name in self.__struct_fields__:
            value = getattr(self, name, None)
            if isinstance(value, PrismaRecord):
                value = value.to_pydantic()
            elif isinstance(value, list):
                value = [item.to_pydantic() if isinstance(item, PrismaRecord) else item for item in value]
            values[name] = value
        return twin.model_construct(**values)


def _pydantic_twin(cls: type) -> Any:
    twin = _PYDANTIC_TWINS.get(cls)
    if twin is None:
        import pydantic

        from ._fastparse import converter_for_spec  # noqa: F401  (spec vocabulary reference)

        def spec_type(spec: Any) -> Any:
            if isinstance(spec, str):
                return {
                    'str': str,
                    'bool': bool,
                    'enum': str,
                    'int': int,
                    'bigint': int,
                    'float': float,
                    'datetime': datetime.datetime,
                    'date': datetime.date,
                    'decimal': decimal.Decimal,
                    'json': Any,
                    'base64': Any,
                }[spec]
            tag, inner = spec
            if tag == 'opt':
                return Optional[spec_type(inner)]
            if tag == 'list':
                return List[spec_type(inner)]
            return Any  # ('model', ...) relations carry nested twins

        fields: Dict[str, Any] = {
            name: (spec_type(spec), None)
            for name, spec in cls.__prisma_fields__.items()  # type: ignore[attr-defined]
        }
        twin = pydantic.create_model(cls.__name__, **fields)
        _PYDANTIC_TWINS[cls] = twin
    return twin
