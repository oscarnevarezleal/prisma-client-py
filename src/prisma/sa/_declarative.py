"""Emit declarative ORM source for a Prisma schema.

`_build.build_metadata()` produces SQLAlchemy Core `Table` objects, which is
what a query layer wants but not what a team adopting SQLAlchemy wants to read
or check in. This module renders the *same* schema as declarative classes ---
`class Account(Base)` with `Mapped[...]` annotations, `mapped_column()` and
`relationship()` --- as Python source text, so it lands in a pull request and
gets reviewed like code.

The one rule that makes a second representation safe
----------------------------------------------------

Every DDL-affecting decision here is **read off the `MetaData` that
`build_metadata()` produced**, never recomputed. Column types, nullability,
server defaults, sequences, index sort order, foreign key actions and the
63-character identifier truncation are all taken from the built objects. The ORM
layer --- class names, attribute names, relationships --- is the only thing
derived from the schema directly, and none of it reaches the DDL.

`tests/test_sqlalchemy/test_declarative.py` asserts that: the emitted source is
executed and its `Base.metadata` compared to `build_metadata()`, table by table,
as compiled DDL, and then both are created in real databases and compared with
`pg_dump`. A declarative layer that drifts from the Core layer is worse than
none, because it looks authoritative.

Attribute names are the **Prisma field names**, not the column names, so call
sites read the same after the migration as before: `account.slug`, not
`account.url_slug`.

Refusals
--------

Some shapes cannot be mapped without changing behaviour. They are never guessed
at. The attribute is not emitted, a comment in its place says why, and the
reason comes back in `DeclarativeSource.refusals`; `strict=True` raises instead.
A missing attribute fails loudly at the call site. A guessed one returns the
wrong rows.
"""

from __future__ import annotations

import re
import keyword
from typing import Any, Set, Dict, List, Tuple, Mapping, Iterable, Optional, Sequence, NamedTuple

import sqlalchemy as sa
from sqlalchemy.sql import operators
from sqlalchemy.types import TypeEngine
from sqlalchemy.dialects import postgresql

from ._build import build_metadata

__all__ = (
    'Refusal',
    'DeclarativeSource',
    'UnsupportedShapeError',
    'build_declarative',
)

_INDENT = '    '
_LINE_LENGTH = 120

#: Attribute names the declarative machinery owns. A Prisma field called
#: `metadata` shadows `Base.metadata`, and the mapping then fails at import with
#: an error that names neither the model nor the field.
_RESERVED_ATTRIBUTES = frozenset({'metadata', 'registry', 'awaitable_attrs'})

#: A JSON column holds any JSON value; `python_type` narrows it to `dict`.
_JSON_TYPES = (postgresql.JSON, postgresql.JSONB, sa.JSON)

_BUILTIN_TYPE_NAMES = {str: 'str', int: 'int', float: 'float', bool: 'bool', bytes: 'bytes'}

#: qualified python type -> the module the annotation needs imported
_ANNOTATION_IMPORTS = {
    'decimal.Decimal': 'decimal',
    'datetime.datetime': 'datetime',
    'datetime.date': 'datetime',
    'datetime.time': 'datetime',
    'datetime.timedelta': 'datetime',
    'uuid.UUID': 'uuid',
}

_NESTED_TYPE_RE = re.compile(r'\b([A-Z][A-Za-z0-9_]*)\(')


class Refusal(NamedTuple):
    """A shape the emitter would have had to guess at, so did not emit."""

    #: the Prisma model the refusal belongs to
    model: str
    #: the field, for a relation refusal; `None` when the whole model is refused
    field: Optional[str]
    #: `model` or `relation`
    kind: str
    #: the relation shape, or `model`
    shape: str
    reason: str

    def describe(self) -> str:
        where = self.model if self.field is None else f'{self.model}.{self.field}'
        return f'{where} ({self.shape}): {self.reason}'


class DeclarativeSource(NamedTuple):
    #: the module source text, ready to write to a file
    source: str
    refusals: Tuple[Refusal, ...]


class UnsupportedShapeError(NotImplementedError):
    """Raised by `build_declarative(strict=True)` when anything was refused."""

    def __init__(self, refusals: Sequence[Refusal]) -> None:
        # annotated rather than inferred: this class is exported from
        # `prisma.sa`, so `pyright --verifytypes` holds the attribute to the
        # same standard as any other part of the public interface
        self.refusals: Tuple[Refusal, ...] = tuple(refusals)
        details = '\n'.join(f'  - {refusal.describe()}' for refusal in self.refusals)
        super().__init__(
            'These schema shapes cannot be expressed as declarative models without\n'
            'changing behaviour, so nothing was emitted for them:\n'
            f'{details}\n'
            'Re-run without `strict` to emit the rest; the refusals come back in\n'
            '`DeclarativeSource.refusals`.'
        )


def build_declarative(
    schema: Mapping[str, Any],
    enums: Mapping[str, Any],
    provider: str,
    *,
    strict: bool = False,
) -> DeclarativeSource:
    """Render declarative ORM source for `schema`.

    `schema` and `enums` are `metadata.SCHEMA` / `metadata.ENUM_SCHEMA` from a
    client generated with `schemaMetadata = true` --- the same inputs
    `build_metadata()` takes, which is what this calls to decide every
    DDL-affecting detail.
    """
    return _Emitter(schema, enums, provider).run(strict=strict)


# -- source-text helpers -----------------------------------------------------


def _lit(value: str) -> str:
    """A string literal spelled the way `ruff format` spells it.

    The emitted file is meant to be checked in, so it has to survive the
    project's formatter unchanged or every regeneration is a spurious diff.
    """
    escaped = value.replace('\\', '\\\\')
    if "'" in escaped and '"' not in escaped:
        return f'"{escaped}"'
    return "'" + escaped.replace("'", "\\'") + "'"


def _call(func: str, args: Sequence[str], indent: str = '', prefix: str = '') -> str:
    """`func(a, b)`, exploded one argument per line when it would not fit.

    `prefix` is whatever already sits on the line ahead of the call, e.g. the
    `name: Mapped[str] = ` of an assignment; without it the width is measured
    against the wrong budget and `ruff format` reflows the result.

    The trailing comma on the exploded form is load-bearing: it is the magic
    trailing comma that stops `ruff format` collapsing the call again.
    """
    single = f'{func}({", ".join(args)})'
    if len(indent) + len(prefix) + len(single) <= _LINE_LENGTH:
        return single

    inner = indent + _INDENT
    body = ''.join(f'{inner}{arg},\n' for arg in args)
    return f'{func}(\n{body}{indent})'


def _type_alias(type_: Any) -> str:
    """Which import alias renders this exact type class.

    Matched by identity, not by name. `postgresql.TIMESTAMP` and `sa.TIMESTAMP`
    are different classes and only the first accepts a precision, so resolving
    the name against `sqlalchemy` first would emit `sa.TIMESTAMP(precision=3)`
    and raise `TypeError` in the user's own file.
    """
    cls = type(type_)
    for alias, module in (('sa', sa), ('postgresql', postgresql)):
        if getattr(module, cls.__name__, None) is cls:
            return alias
    raise NotImplementedError(  # pragma: no cover - every type in _types.py is reachable
        f'Cannot render the SQLAlchemy type {cls.__module__}.{cls.__name__} as source: it is not\n'
        '  reachable as `sqlalchemy.<name>` or `sqlalchemy.dialects.postgresql.<name>`.'
    )


def _qualify_nested(text: str) -> str:
    """Qualify type names nested inside a `repr()`, e.g. `(astext_type=Text())`.

    Only reached for a type another type holds, where there is no object to
    match by identity --- so this resolves by name, preferring `sqlalchemy`.
    """

    def replace(match: 're.Match[str]') -> str:
        name = match.group(1)
        for alias, module in (('sa', sa), ('postgresql', postgresql)):
            candidate = getattr(module, name, None)
            if isinstance(candidate, type) and issubclass(candidate, TypeEngine):
                return f'{alias}.{name}('
        raise NotImplementedError(  # pragma: no cover - only JSON's astext_type reaches this
            f'Cannot render the nested SQLAlchemy type {name!r} as source.'
        )

    return _NESTED_TYPE_RE.sub(replace, text)


def _creation_order(item: Any) -> int:
    """SQLAlchemy's own DDL ordering for table constraints.

    `Table.constraints` is a set; `CreateTable` emits it in creation order. The
    Core metadata creates foreign keys in a fixed order, so rendering them in
    any other order changes the compiled DDL and the equivalence check fails on
    something that is not a real difference.
    """
    return int(getattr(item, '_creation_order', 0))


class _Emitter:
    def __init__(self, schema: Mapping[str, Any], enums: Mapping[str, Any], provider: str) -> None:
        self.schema = schema
        self.enums = enums
        self.metadata = build_metadata(schema, enums, provider)
        self.refusals: List[Refusal] = []
        #: enum database name -> the module-level variable holding its ENUM type
        self.enum_vars: Dict[str, str] = {}
        #: table name -> the module-level variable, for tables with no class
        self.table_vars: Dict[str, str] = {}
        #: imports the rendered annotations need
        self.modules: Set[str] = set()
        self.typing_names: Set[str] = set()
        self.uses_postgresql = False
        self.uses_event = False
        self.uses_mapped = False
        self.uses_relationship = False
        self.uses_foreign = False

    def run(self, *, strict: bool) -> DeclarativeSource:
        mappable = self._plan_models()
        relations = self._plan_relations(mappable)

        blocks: List[str] = []
        blocks.extend(self._render_enum_classes())
        blocks.append(
            'class Base(DeclarativeBase):\n'
            f'{_INDENT}"""Declarative base holding the metadata for every model below."""'
        )
        blocks.extend(self._render_enum_types())
        blocks.extend(self._render_loose_tables(mappable))

        for model in self.schema:
            if model in mappable:
                blocks.extend(self._render_model(model, mappable, relations))

        if strict and self.refusals:
            raise UnsupportedShapeError(self.refusals)

        return DeclarativeSource(
            source=self._render_header() + '\n\n\n' + '\n\n\n'.join(blocks) + '\n',
            refusals=tuple(self.refusals),
        )

    # -- planning ------------------------------------------------------------

    def _plan_models(self) -> Dict[str, str]:
        """model name -> class name, for the models that can be mapped."""
        mappable: Dict[str, str] = {}
        enum_names = set(self.enums)

        for model, spec in self.schema.items():
            reason = self._model_refusal(model, spec, enum_names)
            if reason is None:
                mappable[model] = model
            else:
                self.refusals.append(Refusal(model=model, field=None, kind='model', shape='model', reason=reason))
        return mappable

    def _model_refusal(self, model: str, spec: Mapping[str, Any], enum_names: Set[str]) -> Optional[str]:
        if not spec['primary_key']['columns']:
            return (
                'the model has no primary key. Prisma allows a model identified only by a unique '
                'constraint; the SQLAlchemy ORM cannot map a class without one, and inventing a key '
                'would change which rows the identity map treats as the same row. Emitted as a Core '
                'Table instead, so the DDL is unchanged'
            )
        if model in enum_names:
            return (
                f'a Prisma enum is also named {model!r}, so the model class and the enum class would '
                'collide in the emitted module'
            )

        attributes = list(spec['fields']) + list(spec['relations'])
        bad = sorted(name for name in attributes if name in _RESERVED_ATTRIBUTES or keyword.iskeyword(name))
        if bad:
            return (
                f'the field(s) {", ".join(bad)} cannot be declarative attribute names --- they are '
                'either Python keywords or names the declarative machinery owns. Renaming them would '
                'change every call site, so this is left as a Core Table'
            )
        return None

    def _plan_relations(self, mappable: Mapping[str, str]) -> Dict[Tuple[str, str], bool]:
        """(model, field) -> whether the relationship can be emitted."""
        emit: Dict[Tuple[str, str], bool] = {}

        for model in mappable:
            for field, rel in self.schema[model]['relations'].items():
                reason = self._relation_refusal(rel, mappable)
                emit[(model, field)] = reason is None
                if reason is not None:
                    self.refusals.append(
                        Refusal(model=model, field=field, kind='relation', shape=rel['shape'], reason=reason)
                    )

        # `back_populates` has to name an attribute that exists, so a refusal on
        # one side takes the other side with it.
        for (model, field), allowed in sorted(emit.items()):
            if not allowed:
                continue
            rel = self.schema[model]['relations'][field]
            back = rel['back_field']
            if back is not None and emit.get((rel['to'], back), True) is False:
                emit[(model, field)] = False
                self.refusals.append(
                    Refusal(
                        model=model,
                        field=field,
                        kind='relation',
                        shape=rel['shape'],
                        reason=(
                            f'the other side of the relation, {rel["to"]}.{back}, was refused, and '
                            '`back_populates` has to name an attribute that exists'
                        ),
                    )
                )
        return emit

    def _relation_refusal(self, rel: Mapping[str, Any], mappable: Mapping[str, str]) -> Optional[str]:
        if rel['to'] not in mappable:
            return f'the related model {rel["to"]} could not be mapped'
        if rel.get('join_ambiguous'):
            return (
                "which of the join table's two columns (A or B) this side traverses is not "
                'recoverable from the DMMF --- the shape is a self-referential implicit '
                'many-to-many. `relationship()` needs explicit primaryjoin/secondaryjoin conditions '
                'to say which, and guessing reverses the direction of the relation, which silently '
                'returns the wrong rows'
            )
        return None

    # -- header --------------------------------------------------------------

    def _render_header(self) -> str:
        lines = [
            '"""SQLAlchemy declarative models generated from a Prisma schema.',
            '',
            'Generated by `prisma.sa`. Regenerate it from the Prisma schema, or take',
            'ownership of the file and stop regenerating it --- not both.',
            '',
            'Attribute names are the Prisma field names, so call sites read the same as they',
            'did through the Prisma client. Table and column names are the mapped names, so',
            'the DDL is unchanged: `Account.slug` is the column `accounts.url_slug`.',
            '',
            'The `Mapped[...]` annotations describe what SQLAlchemy hands back, which is not',
            'always what the Prisma client did --- a `@db.Uuid` column comes back as a',
            '`uuid.UUID`, not a `str`. That difference is real, and the annotations say so',
            'rather than hiding it.',
            '"""',
            '',
            'from __future__ import annotations',
        ]

        # Only what the body turned out to need: an unused import is an `F401`
        # in the project this file is checked into.
        stdlib = [f'import {module}' for module in sorted(self.modules, key=_by_length)]
        if self.typing_names:
            stdlib.append(f'from typing import {", ".join(sorted(self.typing_names, key=_by_length))}')
        if stdlib:
            lines.append('')
            lines.extend(stdlib)

        # ordered the way this project's isort settings leave them --- length
        # first, and classes before functions within an import
        orm = ['Mapped'] if self.uses_mapped else []
        orm.append('DeclarativeBase')
        if self.uses_foreign:
            orm.append('foreign')
        if self.uses_relationship:
            orm.append('relationship')
        if self.uses_mapped:
            orm.append('mapped_column')

        lines.extend(['', 'import sqlalchemy as sa'])
        if self.uses_event:
            lines.append('from sqlalchemy import event')
        lines.append(f'from sqlalchemy.orm import {", ".join(orm)}')
        if self.uses_postgresql:
            lines.append('from sqlalchemy.dialects import postgresql')
        return '\n'.join(lines)

    # -- enums ---------------------------------------------------------------

    def _render_enum_classes(self) -> List[str]:
        blocks: List[str] = []
        for name, spec in self.enums.items():
            self.modules.add('enum')
            lines = [
                f'class {name}(str, enum.Enum):',
                f'{_INDENT}"""Prisma enum `{name}`.',
                '',
                f'{_INDENT}Members carry the **stored** label rather than the Prisma name --- `@map` is',
                f'{_INDENT}what makes those differ --- so they can be assigned straight to a column.',
                f'{_INDENT}"""',
                '',
            ]
            lines.extend(f'{_INDENT}{member} = {_lit(label)}' for member, label in spec['values'].items())
            blocks.append('\n'.join(lines))
        return blocks

    def _render_enum_types(self) -> List[str]:
        blocks: List[str] = []
        for name, spec in self.enums.items():
            variable = _unique(f'_{name.lower()}_enum', self.enum_vars.values())
            self.enum_vars[spec['db_name']] = variable
            self.uses_postgresql = True
            args = [_lit(label) for label in spec['values'].values()]
            args.extend([f'name={_lit(spec["db_name"])}', 'metadata=Base.metadata', 'create_type=True'])
            blocks.append(f'{variable} = ' + _call('postgresql.ENUM', args, prefix=f'{variable} = '))
        return blocks

    # -- tables without a class ----------------------------------------------

    def _table(self, name: str) -> sa.Table:
        qualified = f'{self.metadata.schema}.{name}' if self.metadata.schema else name
        return self.metadata.tables[qualified]

    def _render_loose_tables(self, mappable: Mapping[str, str]) -> List[str]:
        """Tables with no class: implicit m2m join tables, and refused models.

        These still have to reach the metadata, or the emitted schema is not the
        one `build_metadata()` describes.
        """
        mapped = {self.schema[model]['table'] for model in mappable}
        blocks: List[str] = []

        for table in self.metadata.tables.values():
            if table.name in mapped:
                continue
            variable = _unique('t_' + re.sub(r'\W', '_', table.name).lstrip('_'), self.table_vars.values())
            self.table_vars[table.name] = variable
            blocks.append(self._render_table(table, variable))
        return blocks

    def _render_table(self, table: sa.Table, variable: str) -> str:
        args = [_lit(table.name), 'Base.metadata']
        args.extend(_call('sa.Column', self._column_args(column, include_name=True), _INDENT) for column in table.c)
        args.extend(self._render_constraint(constraint) for constraint in self._explicit_constraints(table))

        block = f'{variable} = ' + _call('sa.Table', args, prefix=f'{variable} = ')
        indexes = self._render_indexes(table, f'{variable}.c')
        if indexes:
            block += '\n' + '\n'.join(indexes)
        return block

    # -- models --------------------------------------------------------------

    def _render_model(
        self,
        model: str,
        mappable: Mapping[str, str],
        relations: Mapping[Tuple[str, str], bool],
    ) -> List[str]:
        spec = self.schema[model]
        table = self._table(spec['table'])
        lines = [f'class {mappable[model]}(Base):', f'{_INDENT}__tablename__ = {_lit(table.name)}']

        constraints = [self._render_constraint(c, _INDENT * 2) for c in self._explicit_constraints(table)]
        if constraints:
            body = ''.join(f'{_INDENT * 2}{constraint},\n' for constraint in constraints)
            lines.append(f'{_INDENT}__table_args__ = (\n{body}{_INDENT})')

        lines.append('')
        for field, field_spec in spec['fields'].items():
            column = table.c[field_spec['column']]
            self.uses_mapped = True
            args = self._column_args(column, include_name=column.name != field)
            prefix = f'{field}: Mapped[{self._annotation(column)}] = '
            lines.append(f'{_INDENT}{prefix}{_call("mapped_column", args, _INDENT, prefix)}')

        rendered_relations = [
            line
            for field in spec['relations']
            for line in self._render_relation(model, field, mappable, allowed=relations[(model, field)])
        ]
        if rendered_relations:
            lines.append('')
            lines.extend(rendered_relations)

        blocks = ['\n'.join(lines)]
        statements = self._render_indexes(table, f'{mappable[model]}.__table__.c')
        statements.extend(self._render_sequence_ownership(model, mappable[model], table))
        if statements:
            blocks.append('\n'.join(statements))
        return blocks

    # -- columns -------------------------------------------------------------

    def _column_args(self, column: sa.Column[Any], *, include_name: bool) -> List[str]:
        args: List[str] = []
        if include_name:
            args.append(_lit(column.name))
        args.append(self._render_type(column.type))

        default = column.default
        if isinstance(default, sa.Sequence):
            args.append(
                _call(
                    'sa.Sequence',
                    [
                        _lit(str(default.name)),
                        f'data_type={self._render_type(default.data_type)}',
                        'metadata=Base.metadata',
                    ],
                )
            )

        if column.primary_key:
            args.append('primary_key=True')
        args.append(f'nullable={column.nullable}')
        if column.autoincrement is True or (column.primary_key and isinstance(column.type, sa.Integer)):
            # `autoincrement='auto'` would make any integer primary key a SERIAL;
            # Prisma only does that for `@default(autoincrement())`
            args.append(f'autoincrement={column.autoincrement}')

        server_default = column.server_default
        if server_default is not None:
            assert isinstance(server_default, sa.DefaultClause), 'build_metadata only emits DefaultClause'
            args.append(f'server_default=sa.text({_lit(str(server_default.arg))})')
        return args

    def _render_type(self, type_: Any) -> str:
        if isinstance(type_, postgresql.ENUM):
            self.uses_postgresql = True
            # `name` is Optional on the base type; every enum `_build` creates
            # is named, and an unnamed one has no DDL to be equivalent to
            assert type_.name is not None, 'build_metadata names every enum type'
            return self.enum_vars[type_.name]
        if isinstance(type_, postgresql.ARRAY):
            self.uses_postgresql = True
            return f'postgresql.ARRAY({self._render_type(type_.item_type)})'

        name = type(type_).__name__
        alias = _type_alias(type_)
        if alias == 'postgresql':
            self.uses_postgresql = True
        rendered = repr(type_)
        assert rendered.startswith(f'{name}('), f'unexpected repr for {name}: {rendered}'
        return f'{alias}.{name}' + _qualify_nested(rendered[len(name) :])

    def _annotation(self, column: sa.Column[Any]) -> str:
        inner = self._python_type(column.type)
        if column.nullable:
            self.typing_names.add('Optional')
            return f'Optional[{inner}]'
        return inner

    def _python_type(self, type_: Any) -> str:
        if isinstance(type_, postgresql.ARRAY):
            self.typing_names.add('List')
            return f'List[{self._python_type(type_.item_type)}]'
        if isinstance(type_, postgresql.ENUM):
            # the column holds the stored label, and the emitted `str` enum's
            # members *are* those labels, so they assign straight through
            return 'str'
        if isinstance(type_, _JSON_TYPES):
            self.typing_names.add('Any')
            return 'Any'

        python_type = type_.python_type
        builtin = _BUILTIN_TYPE_NAMES.get(python_type)
        if builtin is not None:
            return builtin

        qualified = f'{python_type.__module__}.{python_type.__qualname__}'
        module = _ANNOTATION_IMPORTS[qualified]
        self.modules.add(module)
        return qualified

    # -- constraints and indexes ---------------------------------------------

    def _explicit_constraints(self, table: sa.Table) -> List[Any]:
        """The constraints that have to be spelled out, in a stable order.

        The primary key is left implicit unless `@@id(map:)` named it: the
        columns already carry `primary_key=True`, and an unnamed
        `PrimaryKeyConstraint` alongside them would be noise.
        """
        constraints: List[Any] = []
        if table.primary_key.name is not None:
            constraints.append(table.primary_key)
        foreign_keys = [c for c in table.constraints if isinstance(c, sa.ForeignKeyConstraint)]
        constraints.extend(sorted(foreign_keys, key=_creation_order))
        return constraints

    def _render_constraint(self, constraint: Any, indent: str = '') -> str:
        if isinstance(constraint, sa.PrimaryKeyConstraint):
            args = [_lit(column.name) for column in constraint.columns]
            args.append(f'name={_lit(str(constraint.name))}')
            return _call('sa.PrimaryKeyConstraint', args, indent)

        assert isinstance(constraint, sa.ForeignKeyConstraint), 'build_metadata emits no other constraints'
        args = [
            '[' + ', '.join(_lit(name) for name in constraint.column_keys) + ']',
            '[' + ', '.join(_lit(element.target_fullname) for element in constraint.elements) + ']',
            f'name={_lit(str(constraint.name))}',
            f'ondelete={_lit(str(constraint.ondelete))}',
            f'onupdate={_lit(str(constraint.onupdate))}',
        ]
        return _call('sa.ForeignKeyConstraint', args, indent)

    def _render_indexes(self, table: sa.Table, accessor: str) -> List[str]:
        """`sa.Index(...)` statements, reaching columns through `accessor`.

        Emitted after the class rather than inside `__table_args__` because a
        descending member is `column.desc()` --- an expression, which needs a
        real column object rather than the name a string form would give.
        """
        rendered: List[str] = []
        for index in sorted(table.indexes, key=lambda index: str(index.name)):
            args = [_lit(str(index.name))]
            args.extend(self._render_index_expression(expression, accessor) for expression in index.expressions)
            if index.unique:
                args.append('unique=True')
            args.extend(f'{key}={value!r}' for key, value in sorted(index.dialect_kwargs.items()))
            rendered.append(_call('sa.Index', args))
        return rendered

    def _render_index_expression(self, expression: Any, accessor: str) -> str:
        descending = getattr(expression, 'modifier', None) is operators.desc_op
        column = expression.element if descending else expression
        reference = f'{accessor}[{_lit(column.name)}]'
        return f'{reference}.desc()' if descending else reference

    def _render_sequence_ownership(self, model: str, class_name: str, table: sa.Table) -> List[str]:
        """`ALTER SEQUENCE ... OWNED BY ...`, mirroring `_build._own_sequences`.

        Without it the sequence outlives a dropped column, which is a diff.
        """
        statements: List[str] = []
        for field in self.schema[model]['fields'].values():
            sequence = field.get('sequence')
            if not sequence:
                continue
            ddl = f'ALTER SEQUENCE "{sequence}" OWNED BY "{table.name}"."{field["column"]}"'
            self.uses_event = True
            statements.append(
                'event.listen(\n'
                f'{_INDENT}{class_name}.__table__,\n'
                f'{_INDENT}{_lit("after_create")},\n'
                # SQLAlchemy leaves `DDL.__init__` unannotated
                f'{_INDENT}sa.DDL({_lit(ddl)}),  # type: ignore[no-untyped-call]\n'
                ')'
            )
        return statements

    # -- relationships -------------------------------------------------------

    def _joins_without_a_foreign_key(self, rel: Mapping[str, Any]) -> bool:
        """Whether this relation has to spell its join condition out.

        True exactly when the built `MetaData` carries no `ForeignKeyConstraint`
        for the relation --- which is what `relationMode = "prisma"` produces,
        because Prisma then creates none in the database and enforces relations
        in the query engine instead.
        """
        if rel['shape'] == 'many-to-many':
            table = self._table(rel['join_table'])
            return not [c for c in table.constraints if isinstance(c, sa.ForeignKeyConstraint)]

        table = self._table(self.schema[rel['fk_model']]['table'])
        columns = list(rel['fk_columns'])
        matching = [
            c for c in table.constraints if isinstance(c, sa.ForeignKeyConstraint) and list(c.column_keys) == columns
        ]
        return not matching

    def _primary_join(self, rel: Mapping[str, Any], fk_model: str, referenced: str) -> str:
        """`foreign(Child.parentId) == Parent.id`, as a string SQLAlchemy evals.

        `foreign()` is what tells SQLAlchemy which side of the comparison is the
        dependent one, since there is no `ForeignKey` to say so. It resolves out
        of SQLAlchemy's own namespace when the argument is a string, so it costs
        no import; the many-to-many form below cannot use a string and does.

        The same text serves both sides of the relation: direction is decided by
        the annotation and the mapper, not by which operand comes first.
        """
        pairs = [
            f'foreign({fk_model}.{fk_field}) == {referenced}.{referenced_field}'
            for fk_field, referenced_field in zip(rel['fk_fields'], rel['referenced_fields'])
        ]
        if len(pairs) == 1:
            return pairs[0]
        # a compound `@relation(fields: [a, b], references: [x, y])`
        return 'and_(' + ', '.join(pairs) + ')'

    def _secondary_join_conditions(
        self,
        model: str,
        rel: Mapping[str, Any],
        mappable: Mapping[str, str],
        join_table: str,
    ) -> List[str]:
        """The two halves of a many-to-many join, when the join table has no FKs.

        Emitted as lambdas rather than strings: the join table has no class, so
        there is no name for `relationship()`'s registry lookup to resolve, and
        the module-level `Table` variable is only reachable from real Python.
        A lambda is also evaluated late, so it can name a class defined further
        down the file.
        """
        self_column = rel['join_self_column']
        other_column = rel['join_other_column']
        # `None` on both only happens for a self-referential m2m, which is
        # refused before it reaches here --- assert rather than render `c[None]`.
        assert self_column is not None and other_column is not None, 'an ambiguous join is refused, not rendered'

        this_class = mappable[model]
        other_class = mappable[rel['to']]
        this_key = self.schema[model]['primary_key']['fields'][0]
        other_key = self.schema[rel['to']]['primary_key']['fields'][0]

        self.uses_foreign = True
        return [
            f'primaryjoin=lambda: {this_class}.{this_key} == foreign({join_table}.c[{_lit(self_column)}])',
            f'secondaryjoin=lambda: foreign({join_table}.c[{_lit(other_column)}]) == {other_class}.{other_key}',
        ]

    def _render_relation(
        self,
        model: str,
        field: str,
        mappable: Mapping[str, str],
        *,
        allowed: bool,
    ) -> List[str]:
        rel = self.schema[model]['relations'][field]
        if not allowed:
            refusals = [r for r in self.refusals if r.model == model and r.field == field]
            assert refusals, 'a refused relation always records why'
            return _wrap_comment(f'`{field}` is not emitted: {refusals[0].reason}.', _INDENT)

        target = mappable[rel['to']]
        args = [_lit(target)]

        # `relationship()` infers its join condition from the `ForeignKey`
        # metadata, and under `relationMode = "prisma"` there is none — so it
        # raises `NoForeignKeysError` at `configure_mappers()` rather than
        # loading the wrong rows. Read off the built `MetaData`, like every other
        # DDL-derived decision here, so the two layers cannot disagree about
        # which relations have a constraint.
        explicit_join = self._joins_without_a_foreign_key(rel)

        if rel['shape'] == 'many-to-many':
            join_table = self.table_vars[rel['join_table']]
            args.append(f'secondary={join_table}')
            if explicit_join:
                args.extend(self._secondary_join_conditions(model, rel, mappable, join_table))
        else:
            fk_model = mappable[rel['fk_model']]
            if explicit_join:
                # The referenced side is the model that does *not* hold the
                # foreign key, which on an inverse relation is this one.
                referenced = target if rel['owner'] else mappable[model]
                args.append(f'primaryjoin={_lit(self._primary_join(rel, fk_model, referenced))}')
            else:
                fk_fields = ', '.join(f'{fk_model}.{name}' for name in rel['fk_fields'])
                args.append(f'foreign_keys={_lit(f"[{fk_fields}]")}')
            if rel['to'] == model and not rel['is_list']:
                # a self-relation: without `remote_side` SQLAlchemy cannot tell
                # which end of the join is the parent
                remote = ', '.join(f'{target}.{name}' for name in rel['referenced_fields'])
                args.append(f'remote_side={_lit(f"[{remote}]")}')
            if rel['shape'] == 'to-one-inverse':
                args.append('uselist=False')

        if rel['back_field'] is not None:
            args.append(f'back_populates={_lit(rel["back_field"])}')

        if rel['is_list']:
            self.typing_names.add('List')
            annotation = f'List[{target}]'
        elif rel['nullable']:
            self.typing_names.add('Optional')
            annotation = f'Optional[{target}]'
        else:
            annotation = target

        self.uses_relationship = True
        prefix = f'{field}: Mapped[{annotation}] = '
        return [f'{_INDENT}{prefix}{_call("relationship", args, _INDENT, prefix)}']


def _by_length(name: str) -> Tuple[int, str]:
    """isort's `length-sort`, which this project turns on."""
    return len(name), name


def _unique(preferred: str, taken: Iterable[str]) -> str:
    existing = set(taken)
    index = 2
    candidate = preferred
    while candidate in existing:  # pragma: no cover - needs two names differing only by case
        candidate = f'{preferred}{index}'
        index += 1
    return candidate


def _wrap_comment(text: str, indent: str) -> List[str]:
    """A comment wrapped to the line length; refusal reasons are sentences."""
    lines: List[str] = []
    current = f'{indent}#'
    for word in text.split():
        if len(current) + len(word) + 1 > _LINE_LENGTH:
            lines.append(current)
            current = f'{indent}#'
        current += f' {word}'
    lines.append(current)
    return lines
