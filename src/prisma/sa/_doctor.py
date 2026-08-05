"""Diagnose what a codebase would cost to move from Prisma to SQLAlchemy.

The claim this module makes about any one call site is one of three, and each
has to be traceable to `docs/prisma-to-sqlalchemy-runbook.md`:

``verified``
    §4.1 has a row for it, and §4.1's rows were executed against a live
    PostgreSQL database with both Prisma and SQLAlchemy and compared row for
    row.
``stop``
    §5 has a row for it. The reason reported is that row, not a paraphrase.
``unknown``
    Neither list covers it. This is not a soft ``verified``: an unknown is
    something a human has to look at, and it is counted and named rather than
    quietly folded into either of the other two.

The rule that generates most of the interesting output is that **the method
name is not the classification**. `update` is in §4.1; `update(where=...,
data={'n': {'increment': 1}})` is an atomic operation and therefore §5. Half of
the STOP list is reachable only by reading the arguments, which is why the
scanner hands over argument nodes rather than a count.

The rule that generates most of the *honest* output is that an argument the
scanner cannot see cannot be certified. `find_many(where=filters)` is
``unknown``, not ``verified``: `filters` could hold `{'tags': {'has': x}}`,
which is a STOP. Codebases that build queries dynamically will get a report
that is mostly ``unknown``, and that is the true answer for them.
"""

from __future__ import annotations

import re
import ast
from typing import Any, Set, Dict, List, Tuple, Mapping, Callable, Optional, Sequence, FrozenSet
from pathlib import Path
from dataclasses import field, dataclass

from ._scan import (
    CallSite,
    ScanResult,
    render,
    scan_paths,
    mapping_items,
    sequence_items,
    same_expression,
)

# The same lexer the generator uses to recover `relationMode` from the raw
# schema text, rather than a second regex here: the §0 scope check and the
# metadata builder have to agree about what mode a schema is in, and two
# independent readings of the same annotation is how they stop agreeing.
from ..generator._native_types import parse_relation_mode

__all__ = (
    'Finding',
    'Diagnosis',
    'SchemaFacts',
    'classify',
    'diagnose',
    'diagnose_paths',
    'schema_facts',
    'to_json',
    'render_text',
)

RUNBOOK = 'docs/prisma-to-sqlalchemy-runbook.md'

#: How many call sites the text report lists per status before it says how many
#: more there are. A thousand-call-site codebase produces a thousand-line report
#: otherwise, and the counts — not the listing — are what sizes a migration.
#: `--json` is never truncated.
MAX_LISTED = 40

VERIFIED = 'verified'
STOP = 'stop'
UNKNOWN = 'unknown'

#: What a call site with no §4.1 and no §5 row is pointed at.
NO_REFERENCE = 'none — neither §4.1 nor §5'

#: action -> the §4.1 heading that verifies it.
VERIFIED_ACTIONS: Mapping[str, str] = {
    'find_unique': '§4.1 Reads',
    'find_first': '§4.1 Reads',
    'find_many': '§4.1 Reads',
    'create': '§4.1 Writes — one row, scalar columns',
    'update': '§4.1 Writes — one row, scalar columns',
    'delete': '§4.1 Writes — one row, scalar columns',
    'create_many': '§4.1 Bulk writes',
    'update_many': '§4.1 Bulk writes',
    'delete_many': '§4.1 Bulk writes',
    'upsert': '§4.1 Upsert',
    'count': '§4.1 Aggregates',
    'group_by': '§4.1 Aggregates',
    'tx': '§4.1 Transactions',
    'batch_': '§4.1 Transactions',
}

#: action -> (code, reason, §5 row). Only actions §5 blocks outright; the
#: argument-driven stops are below.
STOP_ACTIONS: Mapping[str, Tuple[str, str, str]] = {
    'aggregate': (
        'aggregate',
        '`aggregate()` (`_sum`, `_avg`, `_min`, `_max`) is not verified',
        '§5 aggregate()',
    ),
}

#: action -> (code, reason). Not blocked, just not covered by either list.
UNKNOWN_ACTIONS: Mapping[str, Tuple[str, str]] = {
    'find_unique_or_raise': ('action-not-covered', '§4.1 verifies `find_unique`, not `find_unique_or_raise`'),
    'find_first_or_raise': ('action-not-covered', '§4.1 verifies `find_first`, not `find_first_or_raise`'),
    'query_raw': (
        'raw-sql',
        '§5 says raw SQL needs no translation — the same text goes to `conn.execute(sa.text(...))` — but no §4.1 '
        'row executed one, and positional `$1` parameters have to become named `:name` binds',
    ),
    'query_first': (
        'raw-sql',
        '§5 says raw SQL needs no translation — the same text goes to `conn.execute(sa.text(...))` — but no §4.1 '
        'row executed one, and positional `$1` parameters have to become named `:name` binds',
    ),
    'execute_raw': (
        'raw-sql',
        '§5 says raw SQL needs no translation — the same text goes to `conn.execute(sa.text(...))` — but no §4.1 '
        'row executed one, and positional `$1` parameters have to become named `:name` binds',
    ),
}

_READ_ARGUMENTS: FrozenSet[str] = frozenset({'where', 'include', 'order', 'take', 'skip', 'distinct'})

#: Arguments §4.1 has a row for, per action. Anything else present on a call is
#: reported ``unknown`` — `cursor=` is the common one, and it has no row.
COVERED_ARGUMENTS: Mapping[str, FrozenSet[str]] = {
    'find_unique': frozenset({'where', 'include'}),
    'find_unique_or_raise': frozenset({'where', 'include'}),
    'find_first': _READ_ARGUMENTS,
    'find_first_or_raise': _READ_ARGUMENTS,
    'find_many': _READ_ARGUMENTS,
    'create': frozenset({'data'}),
    'create_many': frozenset({'data', 'skip_duplicates'}),
    'update': frozenset({'data', 'where'}),
    'update_many': frozenset({'data', 'where'}),
    'upsert': frozenset({'where', 'data', 'include'}),
    'delete': frozenset({'where'}),
    'delete_many': frozenset({'where'}),
    'count': frozenset({'where'}),
    'group_by': frozenset({'by', 'count', 'where', 'sum', 'avg', 'min', 'max'}),
    'tx': frozenset({'max_wait', 'timeout'}),
    'batch_': frozenset(),
    'aggregate': frozenset(),
    'query_raw': frozenset({'query'}),
    'query_first': frozenset({'query'}),
    'execute_raw': frozenset({'query'}),
}

_WHERE_ACTIONS: FrozenSet[str] = frozenset(
    {
        'find_unique',
        'find_unique_or_raise',
        'find_first',
        'find_first_or_raise',
        'find_many',
        'update',
        'update_many',
        'delete',
        'delete_many',
        'count',
        'group_by',
    }
)
_DATA_ACTIONS: FrozenSet[str] = frozenset({'create', 'update', 'update_many'})
_READ_ACTIONS: FrozenSet[str] = frozenset(
    {'find_unique', 'find_unique_or_raise', 'find_first', 'find_first_or_raise', 'find_many'}
)

#: `{'increment': 1}` and friends. §5: "Atomic updates — Not verified."
_ATOMIC_OPERATIONS: FrozenSet[str] = frozenset({'increment', 'decrement', 'multiply', 'divide'})

#: §5: "`connect` / `disconnect` / `set` / `connectOrCreate`".
_CONNECT_OPERATIONS: FrozenSet[str] = frozenset(
    {'connect', 'disconnect', 'set', 'connectOrCreate', 'connect_or_create'}
)

#: §5: "Nested writes (`create: {..., posts: {create: [...]}}`)".
_NESTED_WRITE_OPERATIONS: FrozenSet[str] = frozenset(
    {
        'create',
        'createMany',
        'create_many',
        'update',
        'updateMany',
        'update_many',
        'upsert',
        'delete',
        'deleteMany',
        'delete_many',
    }
)

#: The where-operator rows of §4.1.
_VERIFIED_OPERATORS: FrozenSet[str] = frozenset(
    {
        'not',
        'in',
        'lt',
        'lte',
        'gt',
        'gte',
        'contains',
        'startsWith',
        'starts_with',
        'endsWith',
        'ends_with',
        'mode',
    }
)

#: §4.1 Relation filters — all three compile to a correlated EXISTS.
_RELATION_FILTERS: FrozenSet[str] = frozenset({'some', 'none', 'every', 'is'})

#: Operators that exist in the client and are in neither §4.1 nor §5.
_UNCOVERED_OPERATORS: FrozenSet[str] = frozenset(
    {'equals', 'not_in', 'notIn', 'is_not', 'isNot', 'is_empty', 'isEmpty'}
)

#: §5: "`Json` field filtering (`path`, `string_contains`)".
_JSON_FILTERS: FrozenSet[str] = frozenset(
    {
        'path',
        'string_contains',
        'string_starts_with',
        'string_ends_with',
        'array_contains',
        'array_starts_with',
        'array_ends_with',
    }
)

#: §5: "Scalar list filters (`has`, `hasEvery`, `hasSome`)".
_SCALAR_LIST_FILTERS: FrozenSet[str] = frozenset({'has', 'hasEvery', 'has_every', 'hasSome', 'has_some'})

#: §5: "`aggregate()` (`_sum`, `_avg`, `_min`, `_max`)" — the same functions,
#: reached through `group_by`.
_GROUP_BY_AGGREGATES: FrozenSet[str] = frozenset({'sum', 'avg', 'min', 'max'})

_UPSERT_PRECONDITION_NOTE = (
    '§4.1 upsert precondition: `create` must give the `where` fields the values `where` gives them, '
    'or Prisma round-trips instead of emitting one `INSERT … ON CONFLICT` and the two address different rows'
)

_INCLUDE_NOTE = (
    '`include` has no mechanical equivalent (§4.1 Includes): a to-one include becomes a join, a to-many include '
    'becomes two queries grouped in Python, and emulating a to-many one with a single join multiplies parent rows '
    'and breaks `take`/`skip` — budget a reshape at the call site, not a translation'
)

_MAX_WAIT_NOTE = (
    '`max_wait` maps to `pool_timeout` on `create_engine`, but the defaults are far apart (2s vs 30s) — '
    'set it deliberately, never inherit it (§4.1 Transactions)'
)

_SCHEMA_UNAVAILABLE = (
    'the generated client carries no physical schema metadata; add `schemaMetadata = true` to the '
    'generator block and re-run `prisma generate` (runbook §2)'
)

#: What each `unknown` code means when it is summarised as a caveat.
_CAVEAT_DETAILS: Mapping[str, str] = {
    'opaque-where': 'call sites whose `where` is built elsewhere — §5 blocks `has`/`search`/Json path filters, '
    'and a payload the scanner cannot read could contain one',
    'opaque-data': 'call sites whose write payload is built elsewhere — a nested write or an atomic operation '
    'inside it would be a STOP, and the scanner cannot see one',
    'opaque-arguments': 'call sites that splat `*args`/`**kwargs`, so the arguments are not visible at all',
    'argument-not-covered': 'call sites using an argument no §4.1 row covers, e.g. `cursor=`',
    'where-operator-not-covered': 'call sites using a `where` operator no §4.1 row covers, e.g. `equals`/`not_in`',
    'action-not-covered': 'call sites whose action has no §4.1 row and no §5 row',
    'raw-sql': 'raw SQL call sites; §5 says the SQL passes through unchanged, but the bind parameters do not',
    'dynamic-model': 'call sites whose model is chosen at runtime (`getattr(db, name)`), so neither the table '
    'nor the translation is knowable',
    'unresolved-client': 'call sites whose receiver could not be traced to a `Prisma` client, so the model '
    'attribute may not be a model at all',
    'convention-client': 'call sites matched because the receiver is named `db`/`client`/`prisma`, not because a '
    'binding was found — a naming convention is weaker evidence than a binding',
    'transaction-body-not-visible': 'transactions opened without a `with` block, so which call sites belong to '
    'them could not be determined; §4.1 requires a `tx()` block to migrate whole',
    'unparsed-file': 'files that could not be parsed and were therefore not scanned at all',
}


@dataclass
class Finding:
    """One call site, classified."""

    site: CallSite
    status: str
    code: str
    reason: str
    reference: str
    notes: List[str] = field(default_factory=list)
    #: the Prisma model name, resolved through the schema when it is available
    model: Optional[str] = None


@dataclass
class Check:
    name: str
    #: ``pass``, ``fail`` or ``unknown``
    status: str
    detail: str


@dataclass
class Blocker:
    code: str
    detail: str
    reference: str


@dataclass
class ScopeCheck:
    """The runbook's §0, read from the raw `.prisma` text."""

    paths: List[str] = field(default_factory=list)
    provider: Optional[str] = None
    relation_mode: Optional[str] = None
    native_types: Dict[str, int] = field(default_factory=dict)
    checks: List[Check] = field(default_factory=list)


@dataclass
class SchemaFacts:
    available: bool
    detail: str
    scope: ScopeCheck
    provider: Optional[str] = None
    models: int = 0
    tables: int = 0
    enums: int = 0
    join_tables: int = 0
    relation_shapes: Dict[str, int] = field(default_factory=dict)
    blockers: List[Blocker] = field(default_factory=list)
    #: client attribute (`db.user`) -> model name (`User`)
    model_names: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def unavailable(cls, detail: str, scope: Optional[ScopeCheck] = None) -> 'SchemaFacts':
        return cls(available=False, detail=detail, scope=scope if scope is not None else ScopeCheck())


@dataclass
class GroupSummary:
    key: str
    call_sites: int
    verified: int
    stop: int
    unknown: int
    phase: str


@dataclass
class TransactionBlock:
    block: str
    kind: str
    path: str
    line: int
    members: int
    blocked: bool


@dataclass
class Caveat:
    code: str
    count: int
    detail: str


@dataclass
class Diagnosis:
    findings: List[Finding]
    schema: SchemaFacts
    scan: ScanResult
    modules: List[GroupSummary]
    models: List[GroupSummary]
    transactions: List[TransactionBlock]
    caveats: List[Caveat]
    roots: List[str] = field(default_factory=list)

    def count(self, status: str) -> int:
        return len([finding for finding in self.findings if finding.status == status])


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------


class _Verdicts:
    """Collected reasons, worst-first at the end.

    A visible STOP outranks an unreadable argument: knowing a call site is
    blocked is more useful than knowing it is unclear.
    """

    def __init__(self) -> None:
        self.stops: List[Tuple[str, str, str]] = []
        self.unknowns: List[Tuple[str, str]] = []
        self.notes: List[str] = []

    def stop(self, code: str, reason: str, reference: str) -> None:
        self.stops.append((code, reason, reference))

    def unknown(self, code: str, reason: str) -> None:
        self.unknowns.append((code, reason))


def classify(site: CallSite) -> Finding:
    """Classify one call site against §4.1 and §5."""
    verdicts = _Verdicts()
    action = site.action

    if site.resolution == 'dynamic':
        verdicts.unknown(
            'dynamic-model',
            f'the model is chosen at runtime (`{site.receiver}`), so neither the table nor the translation '
            'is knowable from the source',
        )
    elif site.resolution == 'unresolved':
        verdicts.unknown(
            'unresolved-client',
            f'`{site.receiver}` could not be traced to a Prisma client binding in this module',
        )

    if action in STOP_ACTIONS:
        code, reason, reference = STOP_ACTIONS[action]
        verdicts.stop(code, reason, reference)
    elif action in UNKNOWN_ACTIONS:
        code, reason = UNKNOWN_ACTIONS[action]
        verdicts.unknown(code, reason)

    if site.opaque_arguments:
        splats = ' and '.join(f'`{splat}`' for splat in site.opaque_arguments)
        verdicts.unknown(
            'opaque-arguments',
            f'the call splats {splats}, so its arguments are not visible and no §5 construct can be ruled out',
        )

    _check_arguments(site, verdicts)

    if verdicts.stops:
        code, reason, reference = verdicts.stops[0]
        status = STOP
        extra = [f'also: {other[1]}' for other in verdicts.stops[1:]]
        extra += [f'also: {other[1]}' for other in verdicts.unknowns]
    elif verdicts.unknowns:
        code, reason = verdicts.unknowns[0]
        reference = NO_REFERENCE
        status = UNKNOWN
        extra = [f'also: {other[1]}' for other in verdicts.unknowns[1:]]
    else:
        code, reason = VERIFIED, f'§4.1 verifies `{action}` in the shape this call uses'
        reference = VERIFIED_ACTIONS[action]
        status = VERIFIED
        extra = []

    return Finding(
        site=site,
        status=status,
        code=code,
        reason=reason,
        reference=reference,
        notes=verdicts.notes + extra,
        model=site.model,
    )


def _check_arguments(site: CallSite, verdicts: _Verdicts) -> None:
    action = site.action
    arguments = site.arguments

    if action == 'upsert':
        _check_upsert(site, verdicts)
    else:
        if action in _WHERE_ACTIONS and 'where' in arguments:
            _check_where(arguments['where'], verdicts, 'where')
        if action in _DATA_ACTIONS and 'data' in arguments:
            _check_data(arguments['data'], verdicts, 'data')
        if action == 'create_many' and 'data' in arguments:
            _check_create_many(arguments['data'], verdicts)

    if action in _READ_ACTIONS and 'include' in arguments:
        verdicts.notes.append(_INCLUDE_NOTE)

    if action == 'group_by':
        _check_group_by(arguments, verdicts)

    if action == 'tx':
        _check_transaction(arguments, verdicts)

    covered = COVERED_ARGUMENTS.get(action)
    if covered is None:  # pragma: no cover - every scanned action is in the table
        return

    for name in sorted(set(arguments) - covered):
        verdicts.unknown(
            'argument-not-covered',
            f'no §4.1 row covers `{name}=` on `{action}`',
        )


def _check_transaction(arguments: Mapping[str, ast.expr], verdicts: _Verdicts) -> None:
    if 'timeout' in arguments:
        verdicts.stop(
            'transaction-timeout',
            '`tx(timeout=...)` has no SQLAlchemy equivalent: the query engine enforces it between queries with '
            'its own timer, and a `tx()` blocked on a lock outlives it — `lock_timeout` is a different behaviour',
            '§5 db.tx(timeout=...)',
        )
    if 'max_wait' in arguments:
        verdicts.notes.append(_MAX_WAIT_NOTE)


def _check_group_by(arguments: Mapping[str, ast.expr], verdicts: _Verdicts) -> None:
    used = sorted(_GROUP_BY_AGGREGATES.intersection(arguments))
    if used:
        listed = ', '.join(f'`{name}`' for name in used)
        verdicts.stop(
            'aggregate',
            f'{listed} on `group_by` is the `aggregate()` family (`_sum`, `_avg`, `_min`, `_max`), which is '
            'not verified; only `count=` has a §4.1 row',
            '§5 aggregate()',
        )


def _check_where(node: ast.expr, verdicts: _Verdicts, label: str) -> None:
    items = mapping_items(node)
    if items is None:
        verdicts.unknown(
            'opaque-where',
            f'`{label}` is `{render(node)}`, not a literal, so a §5 filter (`has`, `search`, a Json `path`) '
            'inside it cannot be ruled out',
        )
        return

    for key, value in items:
        if key is None:
            verdicts.unknown('opaque-where', f'`{label}` has a key the scanner cannot read')
            continue

        if key in ('AND', 'OR', 'NOT'):
            elements = sequence_items(value)
            if elements is None:
                elements = [value]
            for element in elements:
                _check_where(element, verdicts, f'{label}[{key!r}]')
            continue

        _check_where_value(key, value, verdicts, label)


def _check_where_value(field_name: str, node: ast.expr, verdicts: _Verdicts, label: str) -> None:
    items = mapping_items(node)
    if items is None:
        # a scalar comparison, or `None` — both are §4.1 rows
        return

    for key, value in items:
        if key is None:
            verdicts.unknown('opaque-where', f'`{label}[{field_name!r}]` has a key the scanner cannot read')
            return

        if key in _JSON_FILTERS:
            verdicts.stop(
                'json-filter',
                f'`{label}` filters `{field_name}` with `{key}`, which is `Json` field filtering and is '
                'not verified',
                '§5 Json field filtering',
            )
            return
        if key in _SCALAR_LIST_FILTERS:
            verdicts.stop(
                'scalar-list-filter',
                f'`{label}` filters `{field_name}` with `{key}`, a scalar list filter, which is not verified',
                '§5 Scalar list filters',
            )
            return
        if key == 'search':
            verdicts.stop(
                'full-text-search',
                f'`{label}` runs a full-text `search` on `{field_name}`, which is not verified',
                '§5 Full-text search',
            )
            return

        if key in _RELATION_FILTERS:
            _check_where(value, verdicts, f'{label}[{field_name!r}][{key!r}]')
        elif key in _UNCOVERED_OPERATORS:
            verdicts.unknown(
                'where-operator-not-covered',
                f'no §4.1 row covers the `{key}` operator (on `{field_name}`)',
            )
        # An unrecognised key here is a compound-unique member — `where={'siteId_slug':
        # {'siteId': a, 'slug': b}}` — which §4.1 does verify. Anything in
        # `_VERIFIED_OPERATORS` needs no comment either.


def _check_create_many(node: ast.expr, verdicts: _Verdicts) -> None:
    elements = sequence_items(node)
    if elements is None:
        verdicts.unknown(
            'opaque-data',
            f'`data` is `{render(node)}`, not a list literal, so a nested write inside a row cannot be ruled out',
        )
        return

    for index, element in enumerate(elements):
        _check_data(element, verdicts, f'data[{index}]')


def _check_data(node: ast.expr, verdicts: _Verdicts, label: str) -> None:
    items = mapping_items(node)
    if items is None:
        verdicts.unknown(
            'opaque-data',
            f'`{label}` is `{render(node)}`, not a literal, so a nested write or an atomic operation inside it '
            'cannot be ruled out',
        )
        return

    for key, value in items:
        if key is None:
            verdicts.unknown('opaque-data', f'`{label}` has a key the scanner cannot read')
            continue

        inner = mapping_items(value)
        if inner is None:
            continue

        operations = [name for name, _ in inner]
        if any(name is None for name in operations):
            verdicts.unknown('opaque-data', f'`{label}[{key!r}]` has a key the scanner cannot read')
            continue

        _check_write_operations(key, [name for name in operations if name is not None], verdicts, label)


def _check_write_operations(field_name: str, operations: Sequence[str], verdicts: _Verdicts, label: str) -> None:
    atomic = sorted(_ATOMIC_OPERATIONS.intersection(operations))
    if atomic:
        listed = ', '.join(atomic)
        verdicts.stop(
            'atomic-update',
            f'`{label}` sets `{field_name}` with an atomic operation ({listed}), which is not verified — '
            '`values_for_update` refuses it by name rather than writing the mapping into the column',
            '§5 Atomic updates',
        )
        return

    connects = sorted(_CONNECT_OPERATIONS.intersection(operations))
    if connects:
        listed = ', '.join(connects)
        verdicts.stop(
            'relation-connect',
            f'`{label}` uses {listed} on `{field_name}`; `disconnect` and `set` are illegal against a NOT NULL '
            'foreign key and none of them is verified',
            '§5 connect / disconnect / set / connectOrCreate',
        )
        return

    nested = sorted(_NESTED_WRITE_OPERATIONS.intersection(operations))
    if nested:
        listed = ', '.join(nested)
        verdicts.stop(
            'nested-write',
            f'`{label}` performs a nested write on `{field_name}` ({listed}); it needs a recursive planner, and '
            '`values_for_create` raises `LookupError` on a relation field rather than half-translating one',
            '§5 Nested writes',
        )
    # Anything else is an ordinary mapping value, i.e. a `Json` column.


def _check_upsert(site: CallSite, verdicts: _Verdicts) -> None:
    arguments = site.arguments

    if 'include' in arguments:
        verdicts.stop(
            'upsert-include',
            '`include=` on `upsert` is not verified, and it is one of the things that makes Prisma round-trip '
            'instead of emitting a single `INSERT … ON CONFLICT`',
            '§5 upsert with include=',
        )

    where = arguments.get('where')
    data = arguments.get('data')
    if where is None or data is None:
        verdicts.unknown('opaque-data', '`upsert` was called without both `where` and `data` visible')
        return

    payloads = mapping_items(data)
    if payloads is None:
        verdicts.unknown(
            'opaque-data',
            f'`data` is `{render(data)}`, not a literal, so neither upsert branch can be read',
        )
        return

    named = {key: value for key, value in payloads if key is not None}
    create = named.get('create')
    update = named.get('update')
    if create is None or update is None:
        verdicts.unknown('opaque-data', '`data` for `upsert` must name both `create` and `update` to be read')
        return

    update_items = mapping_items(update)
    if update_items is None:
        verdicts.unknown('opaque-data', f"`data['update']` is `{render(update)}`, not a literal")
    elif not update_items:
        verdicts.stop(
            'upsert-empty-update',
            'an `upsert` with `update={}` drops out of the single statement and writes nothing — measured, '
            '`@updatedAt` does not move — while `values_for_update({})` still stamps it',
            '§5 upsert with update={}',
        )
    else:
        _check_data(update, verdicts, "data['update']")

    _check_data(create, verdicts, "data['create']")
    _check_where(where, verdicts, 'where')
    _check_upsert_precondition(where, create, verdicts)
    verdicts.notes.append(_UPSERT_PRECONDITION_NOTE)


def _check_upsert_precondition(where: ast.expr, create: ast.expr, verdicts: _Verdicts) -> None:
    """§4.1: `create` must give the `where` fields the values `where` gives them."""
    where_items = mapping_items(where)
    create_items = mapping_items(create)
    if where_items is None or create_items is None:
        verdicts.unknown(
            'opaque-data',
            'the `upsert` precondition (create agrees with where on the unique) could not be checked because '
            'one of the payloads is not a literal',
        )
        return

    required: Dict[str, ast.expr] = {}
    for key, value in where_items:
        if key is None:
            verdicts.unknown('opaque-where', '`where` for `upsert` has a key the scanner cannot read')
            return
        nested = mapping_items(value)
        if nested is None:
            required[key] = value
            continue
        for sub_key, sub_value in nested:
            if sub_key is None:
                verdicts.unknown('opaque-where', '`where` for `upsert` has a key the scanner cannot read')
                return
            required[sub_key] = sub_value

    supplied = {key: value for key, value in create_items if key is not None}
    for name, value in required.items():
        present = supplied.get(name)
        if present is None or not same_expression(present, value):
            verdicts.stop(
                'upsert-where-mismatch',
                f'`create` does not give `{name}` the value `where` gives it, so Prisma round-trips instead of '
                'emitting `INSERT … ON CONFLICT`; measured, the two address different rows',
                '§5 upsert where create disagrees with where',
            )
            return


# ---------------------------------------------------------------------------
# schema facts
# ---------------------------------------------------------------------------


def schema_facts(
    schema_paths: Sequence[Path],
    *,
    available: Optional[bool] = None,
    root: Optional[Path] = None,
) -> SchemaFacts:
    """Facts about the schema: §0's scope check, plus what `build_metadata` sees."""
    scope = _scope_check(schema_paths, root)

    if available is None:
        from .._schema import is_available

        available = is_available()

    if not available:
        return SchemaFacts.unavailable(_SCHEMA_UNAVAILABLE, scope)

    from .._schema import get_schema, get_provider, get_enum_schema

    try:
        schema = get_schema()
        enums = get_enum_schema()
        provider = get_provider()
        from ._build import build_metadata

        metadata = build_metadata(schema, enums, provider)
    except Exception as exc:  # noqa: BLE001 - the failure *is* the finding
        return SchemaFacts.unavailable(f'{type(exc).__name__}: {exc}', scope)

    shapes: Dict[str, int] = {}
    join_tables: Set[str] = set()
    blockers: List[Blocker] = []

    for model, spec in schema.items():
        for name, rel in spec['relations'].items():
            shapes[rel['shape']] = shapes.get(rel['shape'], 0) + 1
            join_table = rel.get('join_table')
            if join_table is not None:
                join_tables.add(join_table)
            if rel['join_ambiguous']:
                blockers.append(
                    Blocker(
                        code='self-referential-m2m',
                        detail=f'{model}.{name} is a self-referential implicit many-to-many; which side is join '
                        'column `A` is not recoverable from the DMMF, so traversing it would be a coin flip',
                        reference='§5 Self-referential implicit m2m',
                    )
                )

    names: Dict[str, str] = {}
    for model in schema:
        instance = model.lower()
        # a collision means the attribute does not identify one model; leaving it
        # out keeps the report from naming the wrong one
        names[instance] = '' if instance in names else model

    return SchemaFacts(
        available=True,
        detail='built from `metadata.SCHEMA` by `build_metadata`',
        scope=scope,
        provider=provider,
        models=len(schema),
        tables=len(metadata.tables),
        enums=len(enums),
        join_tables=len(join_tables),
        relation_shapes=shapes,
        blockers=blockers,
        model_names={key: value for key, value in names.items() if value},
    )


_PROVIDER = re.compile(r'provider\s*=\s*"([^"]+)"')
_DATASOURCE = re.compile(r'datasource\s+\w+\s*\{([^}]*)\}', re.DOTALL)
_NATIVE_TYPE = re.compile(r'@db\.([A-Za-z]+)')


def _scope_check(schema_paths: Sequence[Path], root: Optional[Path]) -> ScopeCheck:
    files = _prisma_files(schema_paths)
    base = root if root is not None else Path.cwd()

    scope = ScopeCheck(paths=[_relative(path, base) for path in files])
    natives: Dict[str, int] = {}

    for path in files:
        try:
            text = _strip_comments(path.read_text(encoding='utf-8'))
        except (UnicodeDecodeError, OSError):  # pragma: no cover - unreadable schema
            continue

        datasource = _DATASOURCE.search(text)
        if datasource is not None:
            provider = _PROVIDER.search(datasource.group(1))
            if provider is not None:
                scope.provider = provider.group(1)

        relation_mode = parse_relation_mode(text)
        if relation_mode is not None:
            scope.relation_mode = relation_mode

        for match in _NATIVE_TYPE.finditer(text):
            natives[match.group(1)] = natives.get(match.group(1), 0) + 1

    scope.native_types = natives
    scope.checks = [
        _provider_check(scope),
        _relation_mode_check(scope, bool(files)),
    ]
    return scope


def _provider_check(scope: ScopeCheck) -> Check:
    if scope.provider is None:
        return Check('provider is PostgreSQL', 'unknown', 'no `datasource` block was read')
    if scope.provider == 'postgresql':
        return Check('provider is PostgreSQL', 'pass', scope.provider)
    return Check(
        'provider is PostgreSQL',
        'fail',
        f'{scope.provider} — the type table has only been verified against PostgreSQL (§0)',
    )


def _relation_mode_check(scope: ScopeCheck, saw_files: bool) -> Check:
    name = 'relationMode'
    if scope.relation_mode == 'prisma':
        return Check(
            name,
            'fail',
            'prisma — the database has no foreign keys at all; Prisma enforces relations in the query '
            'engine, and SQLAlchemy will not. The emitted metadata now matches that database, so this '
            'is no longer a DDL problem: it is a decision about who enforces referential integrity '
            'after the migration (§0)',
        )
    if not saw_files:
        return Check(name, 'unknown', 'no schema was read')
    return Check(name, 'pass', scope.relation_mode or 'absent')


def _prisma_files(schema_paths: Sequence[Path]) -> List[Path]:
    files: List[Path] = []
    for path in schema_paths:
        if path.is_dir():
            files.extend(sorted(path.rglob('*.prisma')))
        else:
            files.append(path)
    return files


def _strip_comments(text: str) -> str:
    """Remove `//` comments, leaving `//` inside a string alone.

    A URL in `url = "postgresql://host/db"` is not a comment, and truncating the
    line there would hide whatever follows it in the block.
    """
    out: List[str] = []
    in_string = False
    index = 0
    while index < len(text):
        char = text[index]
        if char == '"':
            in_string = not in_string
        elif char == '\n':
            in_string = False
        elif not in_string and char == '/' and text[index : index + 2] == '//':
            while index < len(text) and text[index] != '\n':
                index += 1
            continue
        out.append(char)
        index += 1
    return ''.join(out)


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


# ---------------------------------------------------------------------------
# diagnosis
# ---------------------------------------------------------------------------


def diagnose_paths(
    paths: Sequence[Path],
    *,
    root: Path,
    schema_paths: Sequence[Path],
    available: Optional[bool] = None,
) -> Diagnosis:
    scan = scan_paths(paths, root=root)
    facts = schema_facts(schema_paths, available=available, root=root)
    return diagnose(scan, schema=facts, roots=[_relative(path, root) or '.' for path in paths])


def diagnose(scan: ScanResult, *, schema: SchemaFacts, roots: Optional[Sequence[str]] = None) -> Diagnosis:
    findings = [classify(site) for site in scan.call_sites]

    for finding in findings:
        if finding.model is not None:
            finding.model = schema.model_names.get(finding.model, finding.model)

    transactions = _block_transactions(findings)

    return Diagnosis(
        findings=findings,
        schema=schema,
        scan=scan,
        modules=_group(findings, lambda finding: finding.site.path),
        models=_group(findings, lambda finding: finding.model),
        transactions=transactions,
        caveats=_caveats(findings, scan),
        roots=list(roots) if roots is not None else [],
    )


def _block_transactions(findings: List[Finding]) -> List[TransactionBlock]:
    """`tx()` blocks migrate whole, or stay on Prisma whole.

    §5: a transaction spanning a Prisma client *and* a SQLAlchemy connection is
    impossible — measured, not merely unverified. So one STOP inside a block
    takes the rest of the block with it, however clean each of the others is on
    its own.
    """
    members: Dict[str, List[Finding]] = {}
    for finding in findings:
        block = finding.site.block
        if block is not None:
            members.setdefault(block, []).append(finding)

    blocks: List[TransactionBlock] = []
    for block, group in members.items():
        blocked = any(finding.status == STOP for finding in group)
        if blocked:
            for finding in group:
                if finding.status != VERIFIED:
                    continue
                finding.status = STOP
                finding.code = 'transaction-blocked'
                finding.reason = (
                    f'inside `{block}`, which contains a call site with no verified translation; a transaction '
                    'cannot span a Prisma client and a SQLAlchemy connection, so the block migrates whole or '
                    'stays on Prisma whole'
                )
                finding.reference = '§5 A transaction spanning a Prisma client and a SQLAlchemy connection'

        kind, _, rest = block.partition('@')
        path, _, line = rest.rpartition(':')
        blocks.append(
            TransactionBlock(
                block=block,
                kind=kind,
                path=path,
                line=int(line),
                members=len(group),
                blocked=blocked,
            )
        )

    return blocks


def _group(findings: Sequence[Finding], key: Callable[[Finding], Optional[str]]) -> List[GroupSummary]:
    order: List[str] = []
    counts: Dict[str, Dict[str, int]] = {}

    for finding in findings:
        name = key(finding)
        if name is None:
            continue
        if name not in counts:
            order.append(name)
            counts[name] = {VERIFIED: 0, STOP: 0, UNKNOWN: 0}
        counts[name][finding.status] += 1

    summaries: List[GroupSummary] = []
    for name in order:
        tally = counts[name]
        summaries.append(
            GroupSummary(
                key=name,
                call_sites=sum(tally.values()),
                verified=tally[VERIFIED],
                stop=tally[STOP],
                unknown=tally[UNKNOWN],
                phase=_phase(tally[VERIFIED], tally[STOP], tally[UNKNOWN]),
            )
        )
    return summaries


def _phase(verified: int, stop: int, unknown: int) -> str:
    """How a team should read this group when planning a first slice."""
    if stop == 0 and unknown == 0:
        return 'migratable-now'
    if stop == 1 and unknown == 0 and verified > 0:
        return 'one-blocker'
    if stop > verified + unknown:
        return 'blocked'
    return 'mixed'


def _caveats(findings: Sequence[Finding], scan: ScanResult) -> List[Caveat]:
    counts: Dict[str, int] = {}

    if scan.unparsed:
        counts['unparsed-file'] = len(scan.unparsed)

    for finding in findings:
        if finding.status == UNKNOWN:
            counts[finding.code] = counts.get(finding.code, 0) + 1
        if finding.site.resolution == 'convention':
            counts['convention-client'] = counts.get('convention-client', 0) + 1
        if finding.site.entry == 'transaction' and finding.site.block is None:
            counts['transaction-body-not-visible'] = counts.get('transaction-body-not-visible', 0) + 1

    return [Caveat(code=code, count=counts[code], detail=_CAVEAT_DETAILS.get(code, code)) for code in sorted(counts)]


# ---------------------------------------------------------------------------
# output
# ---------------------------------------------------------------------------


def to_json(diagnosis: Diagnosis) -> Dict[str, Any]:
    """The machine-readable report.

    This is an interface: a Claude skill reads it to plan a migration. Keys are
    added, never renamed, and `schema_version` says which shape you have.
    """
    from . import __version__

    return {
        'doctor': {
            'schema_version': 1,
            'sa_version': __version__,
            'runbook': RUNBOOK,
            'statuses': {
                VERIFIED: 'a §4.1 row covers it, executed against a live database',
                STOP: 'a §5 row blocks it; `reason` names the row',
                UNKNOWN: 'neither list covers it — a human has to look',
            },
        },
        'roots': diagnosis.roots,
        'schema': _schema_json(diagnosis.schema),
        'summary': {
            'call_sites': len(diagnosis.findings),
            'verified': diagnosis.count(VERIFIED),
            'stop': diagnosis.count(STOP),
            'unknown': diagnosis.count(UNKNOWN),
            'files_scanned': diagnosis.scan.files_scanned,
            'files_with_call_sites': len({finding.site.path for finding in diagnosis.findings}),
            'unparsed_files': [{'path': entry.path, 'error': entry.error} for entry in diagnosis.scan.unparsed],
            # A summary that only reports what was understood reads as more
            # complete than the scan was. This is the other half.
            'not_analysed': {
                'call_sites': diagnosis.count(UNKNOWN),
                'files': len(diagnosis.scan.unparsed),
                'codes': {caveat.code: caveat.count for caveat in diagnosis.caveats},
            },
        },
        'call_sites': [_finding_json(finding) for finding in diagnosis.findings],
        'modules': [_group_json(group) for group in diagnosis.modules],
        'models': [_group_json(group) for group in diagnosis.models],
        'transactions': [
            {
                'block': block.block,
                'kind': block.kind,
                'path': block.path,
                'line': block.line,
                'members': block.members,
                'blocked': block.blocked,
            }
            for block in diagnosis.transactions
        ],
        'caveats': [
            {'code': caveat.code, 'count': caveat.count, 'detail': caveat.detail} for caveat in diagnosis.caveats
        ],
    }


def _finding_json(finding: Finding) -> Dict[str, Any]:
    site = finding.site
    return {
        'path': site.path,
        'line': site.line,
        'column': site.column,
        'entry': site.entry,
        'action': site.action,
        'receiver': site.receiver,
        'model': finding.model,
        'resolution': site.resolution,
        'block': site.block,
        'arguments': sorted(site.arguments),
        'opaque_arguments': list(site.opaque_arguments),
        'status': finding.status,
        'code': finding.code,
        'reason': finding.reason,
        'reference': finding.reference,
        'notes': list(finding.notes),
    }


def _group_json(group: GroupSummary) -> Dict[str, Any]:
    return {
        'key': group.key,
        'call_sites': group.call_sites,
        'verified': group.verified,
        'stop': group.stop,
        'unknown': group.unknown,
        'phase': group.phase,
    }


def _schema_json(facts: SchemaFacts) -> Dict[str, Any]:
    return {
        'available': facts.available,
        'detail': facts.detail,
        'provider': facts.provider,
        'models': facts.models,
        'tables': facts.tables,
        'enums': facts.enums,
        'join_tables': facts.join_tables,
        'relation_shapes': facts.relation_shapes,
        'blockers': [
            {'code': blocker.code, 'detail': blocker.detail, 'reference': blocker.reference}
            for blocker in facts.blockers
        ],
        'scope_check': {
            'paths': facts.scope.paths,
            'provider': facts.scope.provider,
            'relation_mode': facts.scope.relation_mode,
            'native_types': facts.scope.native_types,
            'checks': [
                {'name': check.name, 'status': check.status, 'detail': check.detail} for check in facts.scope.checks
            ],
        },
    }


_PHASE_HEADINGS = (
    ('migratable-now', 'Migratable now — every call site is verified'),
    ('one-blocker', 'One blocker away — a single STOP in an otherwise verified module'),
    ('mixed', 'Mixed — verified work alongside blockers or unanswered questions'),
    ('blocked', 'Blocked — dominated by call sites with no verified translation'),
)


def render_text(diagnosis: Diagnosis) -> str:
    from . import __version__

    lines: List[str] = []
    lines.append('prisma py sqlalchemy doctor')
    lines.append(f'runbook: {RUNBOOK} — §4.1 is the verified list, §5 is the STOP list')
    lines.append(f'prisma.sa {__version__}')
    lines.append('')

    _render_schema(diagnosis, lines)
    _render_call_sites(diagnosis, lines)
    _render_phases(diagnosis, lines)
    _render_transactions(diagnosis, lines)
    _render_caveats(diagnosis, lines)

    return '\n'.join(lines) + '\n'


def _render_schema(diagnosis: Diagnosis, lines: List[str]) -> None:
    facts = diagnosis.schema

    lines.append('Scope check (§0)')
    for check in facts.scope.checks:
        lines.append(f'  {check.name:<32} {check.status:<8} {check.detail}')
    if facts.scope.native_types:
        listed = ', '.join(f'{name} x{count}' for name, count in sorted(facts.scope.native_types.items()))
        lines.append(f'  {"@db.* annotations":<32} {"":<8} {listed}')
    if facts.scope.paths:
        lines.append(f'  {"schema files":<32} {"":<8} {", ".join(facts.scope.paths)}')
    lines.append('')

    lines.append('Schema (build_metadata)')
    if not facts.available:
        lines.append(f'  unavailable: {facts.detail}')
    else:
        lines.append(
            f'  {facts.models} models, {facts.tables} tables, {facts.enums} enums, '
            f'{facts.join_tables} implicit m2m join tables ({facts.provider})'
        )
        shapes = ', '.join(f'{name} {count}' for name, count in sorted(facts.relation_shapes.items()))
        lines.append(f'  relation shapes: {shapes or "none"}')
        if facts.blockers:
            lines.append('  no verified translation path:')
            for blocker in facts.blockers:
                lines.append(f'    - {blocker.detail} [{blocker.reference}]')
        else:
            lines.append('  no schema-level blockers found')
    lines.append('')


def _render_call_sites(diagnosis: Diagnosis, lines: List[str]) -> None:
    total = len(diagnosis.findings)
    lines.append('Call sites')
    lines.append(
        f'  {total} call sites in {diagnosis.scan.files_scanned} files scanned: '
        f'{diagnosis.count(VERIFIED)} verified, {diagnosis.count(STOP)} stop, '
        f'{diagnosis.count(UNKNOWN)} unknown'
    )
    lines.append('')

    for status, heading in (
        (VERIFIED, 'Verified — a §4.1 row, executed against a live database'),
        (STOP, 'STOP — §5, no verified translation'),
        (UNKNOWN, 'Unknown — neither §4.1 nor §5 covers it'),
    ):
        selected = [finding for finding in diagnosis.findings if finding.status == status]
        lines.append(f'  {heading} ({len(selected)})')
        if not selected:
            lines.append('    none')
        for finding in selected[:MAX_LISTED]:
            site = finding.site
            lines.append(f'    {site.path}:{site.line}  {site.receiver}.{site.action}  [{finding.reference}]')
            if status != VERIFIED:
                lines.append(f'      {finding.reason}')
            for note in finding.notes:
                lines.append(f'      note: {note}')
        if len(selected) > MAX_LISTED:
            lines.append(f'    ... and {len(selected) - MAX_LISTED} more — `--json` lists every one')
        lines.append('')


def _render_phases(diagnosis: Diagnosis, lines: List[str]) -> None:
    lines.append('Phases — what a first slice would cost')
    for phase, heading in _PHASE_HEADINGS:
        selected = [group for group in diagnosis.modules if group.phase == phase]
        lines.append(f'  {heading} ({len(selected)} modules)')
        for group in selected:
            lines.append(
                f'    {group.key}  {group.call_sites} call sites '
                f'({group.verified} verified, {group.stop} stop, {group.unknown} unknown)'
            )
    lines.append('')

    lines.append('  By model')
    for group in diagnosis.models:
        lines.append(
            f'    {group.key}  {group.call_sites} call sites '
            f'({group.verified} verified, {group.stop} stop, {group.unknown} unknown) — {group.phase}'
        )
    if not diagnosis.models:
        lines.append('    none')
    lines.append('')


def _render_transactions(diagnosis: Diagnosis, lines: List[str]) -> None:
    if not diagnosis.transactions:
        return

    lines.append('Transactions — §4.1 requires a block to migrate whole')
    for block in diagnosis.transactions[:MAX_LISTED]:
        state = 'BLOCKED, the whole block stays on Prisma' if block.blocked else 'migratable whole'
        lines.append(f'  {block.path}:{block.line}  {block.kind}()  {block.members} call sites — {state}')
    if len(diagnosis.transactions) > MAX_LISTED:
        lines.append(f'  ... and {len(diagnosis.transactions) - MAX_LISTED} more — `--json` lists every one')
    lines.append('')


def _render_caveats(diagnosis: Diagnosis, lines: List[str]) -> None:
    lines.append('What this report could not analyse')
    if not diagnosis.caveats:
        lines.append('  nothing — every call site found was read in full')
    for caveat in diagnosis.caveats:
        lines.append(f'  {caveat.count} x {caveat.code}: {caveat.detail}')
    for entry in diagnosis.scan.unparsed:
        lines.append(f'    - {entry.path}: {entry.error}')
    lines.append(
        '  the scanner reads one module at a time: a client, a `where` or a write payload that arrives from '
        'another module is not followed, and a call site reached only through a helper is attributed to the '
        'helper, not to its callers'
    )
