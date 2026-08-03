"""A lightweight, pydantic-free record backend (``modelBackend = "slim"``).

Generated record models normally subclass ``pydantic.BaseModel``; pydantic
then compiles a core-schema validator per model at import (or first use).
For records coming back from the query engine — which already enforces the
schema — that validator mostly re-proves invariants the engine guarantees,
and its compiled schemas dominate the memory profile of large clients.

``modelBackend = "slim"`` swaps the generated base for this one:

- ``__slots__`` storage — no ``__dict__``, several times smaller per instance
- deserialization through the compiled conversion plans in ``_fastparse``
  (ISO datetimes, Decimal, Base64, Json, BigInt, nested models); everything
  else passes through untouched
- no import-time or first-use schema compilation at all

Retro-compatibility: ``model_dump()`` / ``dict()`` / ``json()`` match the
pydantic spellings most integrations rely on, and constructing with keyword
arguments applies the same conversions, so ``User(**data)`` keeps working.
This backend intentionally does **not** validate arbitrary input — for
untrusted data keep the default pydantic backend, or validate at the edge.
"""

from __future__ import annotations

import json as _json
import importlib
from typing import Any, Dict, List, Tuple, ClassVar, Optional

from ._fastparse import Converter, converter_for_spec

_PLANS: Dict[type, List[Tuple[str, Optional[Converter]]]] = {}


def _resolver_for(cls: type) -> Any:
    """Resolve sibling model names through the (lazy) generated models package."""
    package = cls.__module__.rsplit('.', 1)[0]

    def resolve(name: str) -> type:
        return getattr(importlib.import_module(package), name)  # type: ignore[no-any-return]

    return resolve


def _plan(cls: type) -> List[Tuple[str, Optional[Converter]]]:
    plan = _PLANS.get(cls)
    if plan is None:
        resolve = _resolver_for(cls)
        fields: Dict[str, Any] = cls.__prisma_fields__  # type: ignore[attr-defined]
        plan = [(name, converter_for_spec(spec, resolve)) for name, spec in fields.items()]
        _PLANS[cls] = plan
    return plan


_FROM_ENGINE: Dict[type, Any] = {}


def _compile_from_engine(cls: type) -> Any:
    """Compile a specialized deserializer for `cls` (unrolled, no loop).

    Generic per-field loops pay tuple unpacking, condition checks and a
    function call per field per row. Unrolling into straight-line code —
    the same trick namedtuple and pydantic v1 use — keeps the per-row cost
    at one function call plus the slot stores, with converters invoked only
    for fields that actually need conversion.
    """
    namespace: Dict[str, Any] = {'_new': object.__new__, '_cls': cls}
    lines = ['def _from_engine(data):', '    i = _new(_cls)', '    g = data.get']
    for idx, (name, converter) in enumerate(_plan(cls)):
        if converter is None:
            lines.append(f'    i.{name} = g({name!r})')
        else:
            namespace[f'_c{idx}'] = converter
            lines.append(f'    v = g({name!r})')
            lines.append(f'    i.{name} = _c{idx}(v) if v is not None else v')
    lines.append('    return i')
    exec('\n'.join(lines), namespace)  # noqa: S102
    impl = namespace['_from_engine']
    _FROM_ENGINE[cls] = impl
    return impl


def _is_optional(spec: Any) -> bool:
    return isinstance(spec, tuple) and spec[0] == 'opt'


def _is_relational(spec: Any) -> bool:
    while isinstance(spec, tuple):
        if spec[0] == 'model':
            return True
        spec = spec[1]
    return False


def scalar_field_names(cls: type) -> List[str]:
    """Field names to select by default — everything except relations."""
    fields: Dict[str, Any] = cls.__prisma_fields__  # type: ignore[attr-defined]
    return [name for name, spec in fields.items() if not _is_relational(spec)]


def related_model(cls: type, field: str) -> type:
    """The model class a relational field points at (loaded lazily)."""
    spec: Any = cls.__prisma_fields__[field]  # type: ignore[attr-defined]
    while isinstance(spec, tuple):
        if spec[0] == 'model':
            return _resolver_for(cls)(spec[1])
        spec = spec[1]
    raise KeyError(f'{field!r} is not a relational field on {cls.__name__}')


class SlimModel:
    __slots__ = ()

    # marker consumed by prisma._compat.model_parse
    __prisma_slim__: ClassVar[bool] = True
    # field name -> conversion spec, emitted by the generator
    __prisma_fields__: ClassVar[Dict[str, Any]] = {}

    def __init__(self, **data: Any) -> None:
        fields = type(self).__prisma_fields__
        for name, converter in _plan(type(self)):
            if name in data:
                value = data.pop(name)
                setattr(self, name, converter(value) if converter is not None else value)
            elif _is_optional(fields[name]):
                setattr(self, name, None)
            else:
                raise TypeError(f'{type(self).__name__} missing required field {name!r}')
        if data:
            unexpected = ', '.join(sorted(data))
            raise TypeError(f'{type(self).__name__} got unexpected fields: {unexpected}')

    @classmethod
    def from_engine(cls, data: Dict[str, Any]) -> 'SlimModel':
        """Build an instance from a trusted query-engine response."""
        impl = _FROM_ENGINE.get(cls)
        if impl is None:
            impl = _compile_from_engine(cls)
        return impl(data)  # type: ignore[no-any-return]

    # -- pydantic-shaped conveniences ------------------------------------- #
    def model_dump(self, *, exclude_none: bool = False) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for name in type(self).__prisma_fields__:
            value = getattr(self, name, None)
            if value is None and exclude_none:
                continue
            if isinstance(value, SlimModel):
                value = value.model_dump(exclude_none=exclude_none)
            elif isinstance(value, list):
                value = [
                    item.model_dump(exclude_none=exclude_none) if isinstance(item, SlimModel) else item
                    for item in value
                ]
            out[name] = value
        return out

    def dict(self, *, exclude_none: bool = False) -> Dict[str, Any]:
        return self.model_dump(exclude_none=exclude_none)

    def model_dump_json(self, *, exclude_none: bool = False) -> str:
        return _json.dumps(self.model_dump(exclude_none=exclude_none), default=str)

    def json(self, *, exclude_none: bool = False) -> str:
        return self.model_dump_json(exclude_none=exclude_none)

    def __repr__(self) -> str:
        fields = ', '.join(f'{name}={getattr(self, name, None)!r}' for name in type(self).__prisma_fields__)
        return f'{type(self).__name__}({fields})'

    def __eq__(self, other: object) -> bool:
        if type(other) is not type(self):
            return NotImplemented
        return all(getattr(self, name, None) == getattr(other, name, None) for name in type(self).__prisma_fields__)

    def __hash__(self) -> int:  # id-based; records are mutable
        return object.__hash__(self)
