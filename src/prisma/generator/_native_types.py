"""Recover annotations Prisma does not put in the DMMF from the raw schema text.

Two of them so far: `@db.*` native types and `@relation(map:)` constraint names.
Both are verified absent from the generator payload, and both change the
database — so both have to be lexed or silently lost.

Prisma does not send these through the generator protocol. Verified, not
assumed: a schema using `@db.Uuid` and `@db.VarChar(255)` produces a DMMF
payload with **zero** `nativeType` keys. The client never needed them — the
query engine parsed `schema.prisma` itself — but anything emitting DDL does, and
without them every `@db.Uuid` column silently becomes `text`.

That is not a cosmetic difference. A field report from a 182-model production
schema found 550 `@db.Uuid` and 20 `@db.VarChar(n)` annotations, covering every
primary and foreign key in the application; translating them to `text` is a
full-database rewrite with index and FK rebuilds on every table.

`GenericData.datamodel` carries the raw schema text — concatenated when
`prismaSchemaFolder` splits it across files — so the annotations are recoverable
by lexing. This module does exactly that and nothing more: it is not a Prisma
parser, and anything it cannot confidently read it reports as absent rather than
guessing.
"""

from __future__ import annotations

import re
from typing import Dict, List, Tuple, Optional

__all__ = ('parse_native_types', 'parse_relation_maps', 'NativeType')

#: (type name, arguments) — e.g. `@db.VarChar(255)` -> ('VarChar', ['255'])
NativeType = Tuple[str, List[str]]

# `model Foo {` ... `}` at the start of a line. Prisma requires the closing brace
# in column 0, which is what makes a brace-free regex safe here.
_MODEL_BLOCK = re.compile(
    r'^model\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\{(?P<body>.*?)^\}',
    re.MULTILINE | re.DOTALL,
)

# a field line: name, type, then attributes. Block attributes start with `@@`
# and are excluded by requiring a leading identifier that is not `@`.
_FIELD_LINE = re.compile(r'^\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s+(?P<rest>\S.*)$')

_NATIVE = re.compile(r'@db\.(?P<type>[A-Za-z_][A-Za-z0-9_]*)\s*(?:\((?P<args>[^)]*)\))?')

_COMMENT = re.compile(r'//.*$', re.MULTILINE)


def _strip_comments(text: str) -> str:
    """Remove `//` comments so an annotation inside one is not picked up.

    Prisma has no block comments. `///` documentation comments are a superset of
    `//` and go the same way.
    """
    return _COMMENT.sub('', text)


def parse_native_types(schema: str) -> Dict[str, Dict[str, NativeType]]:
    """`{model name: {field name: (type, args)}}` for every `@db.*` found.

    Keyed by the names as written in the schema — the *Prisma* model and field
    names, not the mapped table and column names — because that is what the DMMF
    is keyed by and therefore what the caller can join on.
    """
    text = _strip_comments(schema)
    out: Dict[str, Dict[str, NativeType]] = {}

    for block in _MODEL_BLOCK.finditer(text):
        fields: Dict[str, NativeType] = {}

        for line in block.group('body').splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith('@@'):
                continue

            match = _FIELD_LINE.match(line)
            if match is None:
                continue

            native = _parse_annotation(match.group('rest'))
            if native is not None:
                fields[match.group('name')] = native

        if fields:
            out[block.group('name')] = fields

    return out


def _parse_annotation(rest: str) -> Optional[NativeType]:
    match = _NATIVE.search(rest)
    if match is None:
        return None

    raw_args = match.group('args')
    if not raw_args or not raw_args.strip():
        return (match.group('type'), [])

    return (match.group('type'), [arg.strip() for arg in raw_args.split(',') if arg.strip()])


# `@relation(..., map: "name")`. The constraint name Prisma gives the foreign
# key. Absent from the DMMF entirely — verified — so without this the name is
# derived from the table and a schema that bothered to set `map:` gets a
# different constraint. The bug is invisible except where `map:` is doing
# something, which is the only reason anyone writes it.
_RELATION_MAP = re.compile(r'@relation\s*\((?P<args>[^)]*)\)')
_MAP_ARG = re.compile(r'\bmap\s*:\s*"(?P<name>[^"]*)"')


def parse_relation_maps(schema: str) -> Dict[str, Dict[str, str]]:
    """`{model name: {field name: constraint name}}` for every `@relation(map:)`.

    Only relation fields can carry it, and only the side that owns the foreign
    key, so a hit on any other field would be a lexing error rather than a
    finding.
    """
    text = _strip_comments(schema)
    out: Dict[str, Dict[str, str]] = {}

    for block in _MODEL_BLOCK.finditer(text):
        fields: Dict[str, str] = {}

        for line in block.group('body').splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith('@@'):
                continue

            match = _FIELD_LINE.match(line)
            if match is None:
                continue

            relation = _RELATION_MAP.search(match.group('rest'))
            if relation is None:
                continue

            mapped = _MAP_ARG.search(relation.group('args'))
            if mapped is not None:
                fields[match.group('name')] = mapped.group('name')

        if fields:
            out[block.group('name')] = fields

    return out
