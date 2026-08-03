"""Compiled trusted deserializers for query-engine responses.

The query engine already enforces the schema: every value it returns was
read from a database whose shape Prisma manages, serialized by the engine
itself. Running each row through full Pydantic validation re-proves, per
field, facts the engine already guarantees — and that cost scales with
result-set size.

This module compiles, once per model class, a plan that does only the work
JSON cannot represent: ISO strings -> datetime, strings -> Decimal / Base64 /
BigInt, JSON strings -> python objects, nested dicts -> nested model
instances. Everything else is passed through untouched.

Two consumers:

- ``PRISMA_PY_FAST_PARSE=1`` (opt-in, Pydantic v2 only): ``model_parse`` in
  ``_compat`` routes generated record models through :func:`fast_parse`,
  which applies the plan and builds instances with ``model_construct`` —
  the objects are ordinary Pydantic instances, no validation pass.
- the ``modelBackend = "slim"`` generator option, whose pydantic-free model
  classes build their field plans with :func:`converter_for_spec`.

Trust boundary: this is for *engine* responses only. User input, raw JSON
from the network, etc. must keep going through full validation.
"""

from __future__ import annotations

import json
import decimal
import datetime
from enum import Enum
from typing import Any, Dict, List, Type, Tuple, Union, Callable, Optional, get_args, get_origin

Converter = Callable[[Any], Any]

#: field name -> converter, or None where the engine value is passed through.
FieldPlan = List[Tuple[str, Optional[Converter]]]

_PLANS: Dict[type, FieldPlan] = {}


# --------------------------------------------------------------------------- #
# scalar converters
# --------------------------------------------------------------------------- #
def _to_datetime(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return datetime.datetime.fromisoformat(value)
    except ValueError:
        # Python < 3.11 does not accept a trailing 'Z'
        return datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))


def _to_date(value: Any) -> Any:
    return datetime.date.fromisoformat(value) if isinstance(value, str) else value


def _to_int(value: Any) -> Any:
    # BigInt comes back as a string on the GraphQL wire protocol
    return int(value) if isinstance(value, str) else value


def _to_float(value: Any) -> Any:
    return float(value) if isinstance(value, (int, str)) else value


def _to_decimal(value: Any) -> Any:
    return value if isinstance(value, decimal.Decimal) else decimal.Decimal(str(value))


def _to_json(value: Any) -> Any:
    # engine serializes Json columns as JSON strings; validated models store
    # the decoded python object, so match that
    return json.loads(value) if isinstance(value, str) else value


def _to_base64(value: Any) -> Any:
    from .fields import Base64

    return Base64.fromb64(value) if isinstance(value, str) else value


# --------------------------------------------------------------------------- #
# plan compilation
# --------------------------------------------------------------------------- #
def converter_for_annotation(annotation: Any) -> Optional[Converter]:
    """Converter for a resolved type annotation; None means passthrough."""
    origin = get_origin(annotation)

    if origin is Union:
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) != 1:
            return None  # generated models only produce Optional[...]
        inner = converter_for_annotation(args[0])
        if inner is None:
            return None
        return lambda v: None if v is None else inner(v)

    if origin in (list, List):
        item_conv = converter_for_annotation(get_args(annotation)[0])
        if item_conv is None:
            return None
        return lambda v: v if v is None else [item_conv(item) for item in v]

    if annotation is datetime.datetime:
        return _to_datetime
    if annotation is datetime.date:
        return _to_date
    if annotation is decimal.Decimal:
        return _to_decimal
    if annotation is int:
        return _to_int
    if annotation is float:
        return _to_float

    if isinstance(annotation, type):
        if hasattr(annotation, '__prisma_model__'):
            model = annotation
            return lambda v: v if v is None else fast_parse(model, v)
        if issubclass(annotation, Enum):
            # generated models use use_enum_values: validated instances hold
            # the raw value, which is exactly what the engine sends
            return None
        name = annotation.__name__
        if name == 'Base64':
            return _to_base64
        if name == 'Json' or (
            hasattr(annotation, '__mro__') and any(c.__name__ == 'BaseJson' for c in annotation.__mro__)
        ):
            return _to_json

    return None


def _plan_for(model: type) -> FieldPlan:
    plan = _PLANS.get(model)
    if plan is None:
        plan = []
        for name, field in model.model_fields.items():  # type: ignore[attr-defined]
            plan.append((name, converter_for_annotation(field.annotation)))
        _PLANS[model] = plan
    return plan


def fast_parse(model: Type[Any], data: Any) -> Any:
    """dict -> pydantic instance via the compiled plan + model_construct."""
    if not isinstance(data, dict):
        # already an instance (e.g. create() echoing input); fall back
        from ._compat import model_parse_strict

        return model_parse_strict(model, data)

    values: Dict[str, Any] = {}
    for name, converter in _plan_for(model):
        if name in data:
            raw = data[name]
            values[name] = converter(raw) if converter is not None else raw
    return model.model_construct(**values)


# --------------------------------------------------------------------------- #
# spec-based converters (used by the slim model backend)
# --------------------------------------------------------------------------- #
_SCALAR_BY_TAG: Dict[str, Optional[Converter]] = {
    'str': None,
    'bool': None,
    'enum': None,
    'int': _to_int,
    'bigint': _to_int,
    'float': _to_float,
    'datetime': _to_datetime,
    'date': _to_date,
    'decimal': _to_decimal,
    'json': _to_json,
    'base64': _to_base64,
}


def converter_for_spec(spec: Any, resolve_model: Callable[[str], type]) -> Optional[Converter]:
    """Converter for a generated field spec.

    Specs are nested tuples emitted by the generator, e.g.
    ``('opt', ('list', ('model', 'Post')))``; `resolve_model` maps a model
    name to its class (lazily, so unused models are never imported).
    """
    if isinstance(spec, str):
        return _SCALAR_BY_TAG[spec]

    tag, inner = spec
    if tag == 'opt':
        conv = converter_for_spec(inner, resolve_model)
        if conv is None:
            return None
        return lambda v: None if v is None else conv(v)
    if tag == 'list':
        conv_item = converter_for_spec(inner, resolve_model)
        if conv_item is None:
            return None
        return lambda v: v if v is None else [conv_item(item) for item in v]
    if tag == 'model':
        name = inner

        def _convert(value: Any, _name: str = name) -> Any:
            if value is None:
                return None
            cls = resolve_model(_name)
            # `from_engine` is emitted onto the generated model classes, which
            # cannot be named here — the resolver is only known to return a class.
            return cls.from_engine(value)  # type: ignore[attr-defined]

        return _convert

    raise ValueError(f'unknown field spec: {spec!r}')
