"""Recover annotations Prisma does not put in the DMMF from the raw schema text.

Four of them so far: `@db.*` native types, `@relation(map:)` constraint names,
`@relation(onUpdate:)` referential actions and the datasource's `relationMode`.
All four are verified absent from the generator payload, and all four change the
database — so all four have to be lexed or silently lost.

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
from typing import Dict, List, Tuple, Callable, Optional

__all__ = (
    'parse_native_types',
    'parse_relation_maps',
    'parse_relation_on_update',
    'parse_relation_mode',
    'NativeType',
)

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


# `@relation(...)`. Prisma's own parser is line-oriented for field definitions —
# splitting the attribute across lines is rejected with "This line is not a valid
# field or attribute definition" (verified against the 5.19 schema-wasm) — so a
# per-line scan loses nothing. What it must not do is trust `[^)]*`: a `)` inside
# a quoted `map:` name closes the group early, and a quoted string containing
# `map:` or `onUpdate:` is data, not an argument.
_RELATION_CALL = re.compile(r'@relation\s*\(')

_STRING = re.compile(r'"(?P<value>[^"]*)"')

# `map:` takes a string; `onUpdate:` takes a referential action, which is always
# a bare identifier.
_MAP_KEY = re.compile(r'\bmap\s*:\s*')
_ON_UPDATE_ARG = re.compile(r'\bonUpdate\s*:\s*(?P<action>[A-Za-z_][A-Za-z0-9_]*)')


def _relation_args(rest: str) -> Optional[str]:
    """The text between `@relation(` and its matching `)`, or `None`.

    Scanned rather than matched so that quoting is respected: `map: "a)b"` is a
    legal constraint name and a regex stopping at the first `)` would read the
    attribute as unterminated and drop it.
    """
    call = _RELATION_CALL.search(rest)
    if call is None:
        return None

    start = call.end()
    depth = 1
    quoted = False

    for index in range(start, len(rest)):
        char = rest[index]
        if quoted:
            quoted = char != '"'
        elif char == '"':
            quoted = True
        elif char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
            if depth == 0:
                return rest[start:index]

    # An unclosed `@relation(` is not valid Prisma; report absence rather than
    # guessing at where it was meant to end.
    return None


def _blank_strings(args: str) -> str:
    """`args` with the *contents* of every string literal replaced by spaces.

    So that `@relation("onUpdate: Cascade", fields: [...])` — a relation *name*
    that happens to spell an argument — cannot be read as one. Offsets are
    preserved, so a key located in the blanked copy indexes back into the real
    arguments and the value is read from those.
    """
    return _STRING.sub(lambda match: '"' + ' ' * len(match.group('value')) + '"', args)


def _parse_relation_arg(schema: str, read: Callable[[str], Optional[str]]) -> Dict[str, Dict[str, str]]:
    """`{model name: {field name: value}}` for every `@relation` `read` accepts.

    Only relation fields can carry `@relation`, and only the side that owns the
    foreign key, so a hit on any other field would be a lexing error rather than
    a finding.
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

            args = _relation_args(match.group('rest'))
            if args is None:
                continue

            value = read(args)
            if value is not None:
                fields[match.group('name')] = value

        if fields:
            out[block.group('name')] = fields

    return out


def parse_relation_maps(schema: str) -> Dict[str, Dict[str, str]]:
    """`{model name: {field name: constraint name}}` for every `@relation(map:)`.

    The constraint name Prisma gives the foreign key. Absent from the DMMF
    entirely — verified — so without this the name is derived from the table and
    a schema that bothered to set `map:` gets a different constraint. The bug is
    invisible except where `map:` is doing something, which is the only reason
    anyone writes it.
    """
    return _parse_relation_arg(schema, _read_map)


def _read_map(args: str) -> Optional[str]:
    key = _MAP_KEY.search(_blank_strings(args))
    if key is None:
        return None

    # `map:` is only ever followed by a string literal. Anything else is not a
    # constraint name we can read, so report absence rather than guessing.
    value = _STRING.match(args, key.end())
    return value.group('value') if value is not None else None


def parse_relation_on_update(schema: str) -> Dict[str, Dict[str, str]]:
    """`{model name: {field name: action}}` for every `@relation(onUpdate:)`.

    The referential action Prisma applies to the foreign key's `ON UPDATE`.
    Absent from the DMMF exactly like the two above — verified: a field written
    `@relation(..., onUpdate: Restrict, onDelete: Cascade)` arrives carrying
    `relationOnDelete: 'Cascade'` and **no** `relationOnUpdate` key at all.

    Prisma's default is `Cascade` at every arity — unlike `onDelete`, which
    depends on whether the relation is mandatory — so the omission is invisible
    until a schema declares something else, and then the emitted DDL is silently
    wrong.

    Reported as the action name as written (`Restrict`, `NoAction`, ...), not
    SQL; a field that does not declare one is absent, meaning "Prisma's default"
    rather than any particular action.
    """
    return _parse_relation_arg(schema, _read_on_update)


def _read_on_update(args: str) -> Optional[str]:
    match = _ON_UPDATE_ARG.search(_blank_strings(args))
    return match.group('action') if match is not None else None


# `datasource db { ... }`. Same shape as `_MODEL_BLOCK`: Prisma requires the
# closing brace in column 0, and a datasource block holds no nested braces.
_DATASOURCE_BLOCK = re.compile(
    r'^datasource\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\{(?P<body>.*?)^\}',
    re.MULTILINE | re.DOTALL,
)

# `relationMode = "prisma"`, anchored to the start of its own line so that a
# `url` holding the text cannot be read as the setting.
_RELATION_MODE_ASSIGNMENT = re.compile(r'^[ \t]*relationMode\s*=\s*"(?P<mode>[^"]*)"', re.MULTILINE)


def parse_relation_mode(schema: str) -> Optional[str]:
    """The datasource's `relationMode`, or `None` when it does not declare one.

    Absent from the generator payload like the three above — verified: a schema
    with `relationMode = "prisma"` arrives with a `datasources` entry carrying
    `name`, `provider`, `activeProvider`, `url`, `schemas` and `sourceFilePath`,
    and no relation-mode key anywhere in the payload. The only trace is the raw
    schema text.

    Unlike the three above this is a *datasource* setting rather than a field
    attribute, so it is one value for the whole schema rather than a mapping —
    but it is recovered here, with them, because it is the same problem: an
    annotation in the schema language that changes the database and never
    reaches the wire.

    Measured against `prisma db push` 5.19 on PostgreSQL, `relationMode =
    "prisma"` creates **no foreign key constraints at all** — not on model
    tables and not on the implicit many-to-many join tables — and creates no
    index in their place either. Prisma only warns that relation scalars are
    then unindexed.

    Reported as written (`prisma`, `foreignKeys`); `None` means the datasource
    is silent, which is Prisma's default of `foreignKeys`.
    """
    text = _strip_comments(schema)

    for block in _DATASOURCE_BLOCK.finditer(text):
        match = _RELATION_MODE_ASSIGNMENT.search(block.group('body'))
        if match is not None:
            return match.group('mode')

    # Prisma permits exactly one datasource block, so there is no second one to
    # disagree; a schema with none simply has not declared a mode.
    return None
