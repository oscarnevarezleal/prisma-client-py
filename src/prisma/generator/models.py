import os
import sys
import enum
import pprint
import textwrap
import importlib
from typing import (
    TYPE_CHECKING,
    Any,
    Set,
    Dict,
    List,
    Type,
    Tuple,
    Union,
    Generic,
    TypeVar,
    ClassVar,
    Iterable,
    Iterator,
    NoReturn,
    Optional,
    cast,
)
from keyword import iskeyword
from pathlib import Path
from importlib import util as importlib_util, machinery
from itertools import chain
from contextvars import ContextVar
from importlib.abc import InspectLoader
from typing_extensions import Annotated, override

import click
import pydantic
from pydantic.fields import PrivateAttr

from .. import config
from .utils import Faker, Sampler, clean_multiline
from ..utils import DEBUG_GENERATOR, assert_never
from ..errors import UnsupportedListTypeError
from .._compat import (
    PYDANTIC_V2,
    Field as FieldInfo,
    BaseConfig,
    ConfigDict,
    BaseSettings,
    GenericModel,
    PlainSerializer,
    BaseSettingsConfig,
    model_dict,
    model_parse,
    model_rebuild,
    root_validator,
    cached_property,
    field_validator,
)
from .._constants import QUERY_BUILDER_ALIASES
from ._dsl_parser import parse_schema_dsl
from ._native_types import NativeType, parse_native_types

__all__ = (
    'AnyData',
    'PythonData',
    'DefaultData',
    'GenericData',
)

_ModelT = TypeVar('_ModelT', bound=pydantic.BaseModel)

# NOTE: this does not represent all the data that is passed by prisma

ATOMIC_FIELD_TYPES = ['Int', 'BigInt', 'Float']

TYPE_MAPPING = {
    'String': '_str',
    'Bytes': "'fields.Base64'",
    'DateTime': 'datetime.datetime',
    'Boolean': '_bool',
    'Int': '_int',
    'Float': '_float',
    'BigInt': '_int',
    'Json': "'fields.Json'",
    'Decimal': 'decimal.Decimal',
}
FILTER_TYPES = [
    'String',
    'Bytes',
    'DateTime',
    'Boolean',
    'Int',
    'BigInt',
    'Float',
    'Json',
    'Decimal',
]
RECURSIVE_TYPE_DEPTH_WARNING = """Some types are disabled by default due to being incompatible with Mypy, it is highly recommended
to use Pyright instead and configure Prisma Python to use recursive types. To re-enable certain types:"""

RECURSIVE_TYPE_DEPTH_WARNING_DESC = """
generator client {
  provider             = "prisma-client-py"
  recursive_type_depth = -1
}

If you need to use Mypy, you can also disable this message by explicitly setting the default value:

generator client {
  provider             = "prisma-client-py"
  recursive_type_depth = 5
}

For more information see: https://prisma-client-py.readthedocs.io/en/stable/reference/limitations/#default-type-limitations
"""

FAKER: Faker = Faker()


ConfigT = TypeVar('ConfigT', bound=pydantic.BaseModel)

# Although we should just be able to access the config from the datamodel
# we have to do some validation that requires access to the config, this is difficult
# with heavily nested models as our current workaround only sets the datamodel context
# post-validation meaning we cannot access it in validators. To get around this we have
# a separate config context.
# TODO: better solution
data_ctx: ContextVar['AnyData'] = ContextVar('data_ctx')
config_ctx: ContextVar['Config'] = ContextVar('config_ctx')


def get_datamodel() -> 'Datamodel':
    return data_ctx.get().dmmf.datamodel


# typed to ensure the caller has to handle the cases where:
# - a custom generator config is being used
# - the config is invalid and therefore could not be set
def get_config() -> Union[None, pydantic.BaseModel, 'Config']:
    return config_ctx.get(None)


def get_list_types() -> Iterable[Tuple[str, str]]:
    # WARNING: do not edit this function without also editing Field.is_supported_scalar_list_type()
    return chain(
        ((t, TYPE_MAPPING[t]) for t in FILTER_TYPES),
        ((enum.name, f"'enums.{enum.name}'") for enum in get_datamodel().enums),
    )


def sql_param(num: int = 1) -> str:
    # TODO: add case for sqlserver
    active_provider = data_ctx.get().datasources[0].active_provider
    if active_provider == 'postgresql':
        return f'${num}'

    # TODO: test
    if active_provider == 'mongodb':  # pragma: no cover
        raise RuntimeError('no-op')

    # SQLite and MySQL use this style so just default to it
    return '?'


def raise_err(msg: str) -> NoReturn:
    raise TemplateError(msg)


#: PostgreSQL's `NAMEDATALEN - 1`. MySQL allows 64, SQL Server 128; this is the
#: tightest of the relational providers and the one Prisma truncates against.
MAX_IDENTIFIER_LENGTH = 63


def truncate_identifier(base: str, suffix: str, limit: int = MAX_IDENTIFIER_LENGTH) -> str:
    """Prisma's rule for a derived constraint name that is too long.

    The suffix (`_key`, `_idx`, `_fkey`) is what makes the name recognisable, so
    it is kept and the base is cut to fit. Verified against `prisma db push`:

        document_requirements_on_documents_documentId_documentRequirementId_journeyId_key  (81)
        -> document_requirements_on_documents_documentId_documentRequi_key                 (63)

    Emitting the untruncated name does not merely diverge from Prisma —
    SQLAlchemy raises `IdentifierError` before any SQL is sent, so it fails the
    Alembic step outright.
    """
    name = f'{base}{suffix}'
    if len(name) <= limit:
        return name
    return base[: limit - len(suffix)] + suffix


def _native_type_for(field: 'Field', model_natives: Dict[str, NativeType]) -> Optional[List[Any]]:
    native = field.native_type or model_natives.get(field.name)
    if native is None:
        return None
    return [native[0], list(native[1])]


def _sequence_for(model: 'Model', field: 'Field') -> Optional[str]:
    """The sequence backing `@default(autoincrement())`, when it needs naming.

    A primary key gets `SERIAL`, which SQLAlchemy emits from `autoincrement=True`
    and which creates the sequence implicitly. A **non-primary-key** column does
    not: SQLAlchemy emits a plain `integer`, the sequence is never created, and
    because Prisma still marks the column `NOT NULL` every INSERT into that table
    fails.

    Nothing reports this. Alembic's `compare_server_default` is off by default,
    so autogenerate produces an empty diff and the migration passes its own gate
    while leaving a database that rejects writes. Naming the sequence here is
    what lets the builder emit it.

    Verified against `prisma db push`: the name is `<table>_<column>_seq`, and it
    is `OWNED BY` the column.
    """
    default = field.default_spec
    if not default or default.get('kind') != 'generator' or default.get('name') != 'autoincrement':
        return None

    if field.name in model.primary_key_fields:
        return None

    return truncate_identifier('_'.join([model.table_name, field.column_name]), '_seq')


def build_schema_metadata(datamodel: 'Datamodel', schema_text: Optional[str] = None) -> Dict[str, Any]:
    """Reconstruct the physical database schema from the DMMF.

    The generated client has never needed this: it hands a GraphQL-ish document
    to the query engine, and the engine knows the tables. Anything that builds
    SQL itself — a SQLAlchemy backend, a migration tool, a schema differ — needs
    table names, column names, constraints and resolved foreign keys, none of
    which the client currently carries.

    Returned as plain dicts on purpose. It is emitted into generated code as a
    literal, so it must round-trip through `repr()`, and a literal costs one
    dict construction at import rather than N object instantiations.

    `schema_text` is the raw `schema.prisma` contents, needed only for `@db.*`
    native types, which Prisma does not put on the wire. Callers that have it
    should pass it; without it every `@db.Uuid` silently reads as `text`.
    """
    schema: Dict[str, Any] = {}
    native_types = parse_native_types(schema_text) if schema_text else {}

    for model in datamodel.models:
        model_natives = native_types.get(model.name, {})
        fields: Dict[str, Any] = {}
        relations: Dict[str, Any] = {}

        for field in model.all_fields:
            if field.is_relational:
                relations[field.name] = model.relation_metadata(field)
                continue

            fields[field.name] = {
                'column': field.column_name,
                'kind': field.kind,
                'type': field.type,
                'is_list': field.is_list,
                'nullable': not field.is_required,
                'is_id': field.is_id,
                'is_unique': field.is_unique,
                'is_read_only': field.is_read_only,
                'is_updated_at': field.is_updated_at,
                'default': field.default_spec,
                # Prisma does not send `@db.*` on the wire (verified), so this
                # falls back to lexing the raw schema text. `Field.native_type`
                # is checked first so a future Prisma release that does send it
                # takes precedence automatically.
                'native_type': _native_type_for(field, model_natives),
                'sequence': _sequence_for(model, field),
            }

        indexes = []
        for index in datamodel.indexes_for(model.name):
            # `@@id` and `@@unique` also show up here; they are reported
            # separately as constraints, and emitting them twice would make a
            # migration tool create a redundant index alongside each one.
            if index.type in {'id', 'unique'}:
                continue
            columns = [model.resolve_field(f.name).column_name for f in index.fields]
            indexes.append(
                {
                    # An unnamed `@@index` still has a name in the database —
                    # Prisma's default. Resolving it here means a schema differ
                    # compares real names instead of reporting every index as
                    # both dropped and added.
                    'name': index.db_name or index.name or truncate_identifier('_'.join([model.table_name, *columns]), '_idx'),
                    'is_named': index.db_name is not None or index.name is not None,
                    'type': index.type,
                    'algorithm': index.algorithm,
                    'clustered': index.clustered,
                    'columns': columns,
                    'fields': [
                        {
                            'name': f.name,
                            'sort_order': f.sort_order,
                            'length': f.length,
                            'operator_class': f.operator_class,
                        }
                        for f in index.fields
                    ],
                }
            )

        # Driven off `datamodel.indexes` rather than `uniqueIndexes`, because
        # that is the only place a *field-level* `@unique` shows up — Prisma
        # reports those solely as `Field.isUnique`, and a client reading
        # `uniqueIndexes` alone silently loses every single-column unique in the
        # schema.
        uniques = []
        for index in datamodel.indexes_for(model.name):
            if index.type != 'unique':
                continue

            unique_fields = [f.name for f in index.fields]
            columns = [model.resolve_field(name).column_name for name in unique_fields]
            # The Prisma-level identifier, i.e. the key in `where={...}`. A
            # compound unique gets it from `uniqueIndexes`; a field-level one is
            # addressed by the field name itself.
            prisma_name = next(
                (unique.name for unique in model.unique_indexes if set(unique.fields) == set(unique_fields)),
                None,
            )
            uniques.append(
                {
                    # `name` and `db_name` are unrelated namespaces and both are
                    # needed: one addresses the constraint in a query, the other
                    # names it in the database.
                    'name': prisma_name or '_'.join(unique_fields),
                    'db_name': index.db_name or truncate_identifier('_'.join([model.table_name, *columns]), '_key'),
                    'fields': unique_fields,
                    'columns': columns,
                    'is_defined_on_field': index.is_defined_on_field,
                }
            )

        primary_key = model.primary_key_fields
        # `@@id(map: "...")`. Left as None when unmapped: unlike unique and
        # index names, Prisma's default primary key constraint name is
        # provider-specific (`<table>_pkey` on PostgreSQL, `PRIMARY` on MySQL),
        # so deriving one here would bake in the wrong answer for some users.
        primary_key_db_name = next(
            (index.db_name for index in datamodel.indexes_for(model.name) if index.type == 'id'),
            None,
        )
        schema[model.name] = {
            'table': model.table_name,
            'primary_key': {
                'name': (model.compound_primary_key.name if model.compound_primary_key is not None else None),
                'db_name': primary_key_db_name,
                'fields': primary_key,
                'columns': [model.resolve_field(name).column_name for name in primary_key],
            },
            'fields': fields,
            'relations': relations,
            'uniques': uniques,
            'indexes': indexes,
        }

    return schema


def build_enum_metadata(datamodel: 'Datamodel') -> Dict[str, Any]:
    """Enum names and value names as the *database* spells them.

    `@map` on an enum value means the Python name and the stored label differ;
    without this a SQL client writes the Python name and the insert fails.
    """
    return {
        enum.name: {
            'db_name': enum.db_name or enum.name,
            'values': {value.name: value.db_name or value.name for value in enum.values},
        }
        for enum in datamodel.enums
    }


def as_literal(value: object, indent: int = 4) -> str:
    """Render a JSON-ish Python object as an indented source literal.

    Emitted as a literal rather than as constructor calls so that importing the
    generated module costs one dict construction, not N object instantiations —
    the same reason `metadata.py` has always been literals.
    """
    formatted = pprint.pformat(value, indent=1, width=110 - indent, sort_dicts=False)
    return textwrap.indent(formatted, ' ' * indent)


def type_as_string(typ: str) -> str:
    """Ensure a type string is wrapped with a string, e.g.

    enums.Role -> 'enums.Role'
    """
    # TODO: use this function internally in this module
    if not typ.startswith("'") and not typ.startswith('"'):
        return f"'{typ}'"
    return typ


def format_documentation(doc: str, indent: int = 4) -> str:
    """Format a schema comment by indenting nested lines, e.g.

        '''Foo
    Bar'''

    Becomes

        '''Foo
        Bar
        '''
    """
    if not doc:
        # empty string, nothing to do
        return doc

    prefix = ' ' * indent
    first, *rest = doc.splitlines()
    return '\n'.join(
        [
            first,
            *[textwrap.indent(line, prefix) for line in rest],
            prefix,
        ]
    )


def max_relation_chain_depth(models: List['Model']) -> int:
    """Longest simple path through the relation graph (distinct models linked by
    relations). This is how deep Pydantic recurses building a model's validator,
    so it bounds the recursion limit the `recursive_validation_models` runtime
    needs. Tight for common shapes (a linear chain -> N, a star/hub -> 2) and
    always <= the model count, with a budgeted fallback to the model count for
    pathologically dense graphs.
    """
    names = {m.name for m in models}
    adj: Dict[str, Set[str]] = {}
    for model in models:
        targets: Set[str] = set()
        for field in model.all_fields:
            if field.relation_name and field.type in names and field.type != model.name:
                targets.add(field.type)
        adj[model.name] = targets

    n = len(models)
    if n <= 1:
        return n

    best = 1
    budget = 200_000
    for start in adj:
        on_path = {start}
        stack: List[Tuple[str, Iterator[str]]] = [(start, iter(sorted(adj[start])))]
        while stack:
            if len(stack) > best:
                best = len(stack)
            if best >= n:
                return n  # cannot exceed n distinct nodes
            _node, it = stack[-1]
            for nxt in it:
                if nxt not in on_path:
                    budget -= 1
                    on_path.add(nxt)
                    stack.append((nxt, iter(sorted(adj[nxt]))))
                    break
            else:
                on_path.discard(stack.pop()[0])
            if budget <= 0:
                return n  # dense graph: fall back to the safe upper bound
    return best


def _module_spec_serializer(spec: machinery.ModuleSpec) -> str:
    assert spec.origin is not None, 'Cannot serialize module with no origin'
    return spec.origin


def _pathlib_serializer(path: Path) -> str:
    return str(path.absolute())


def _recursive_type_depth_factory() -> int:
    click.echo(
        click.style(
            f'\n{RECURSIVE_TYPE_DEPTH_WARNING}',
            fg='yellow',
        )
    )
    click.echo(f'{RECURSIVE_TYPE_DEPTH_WARNING_DESC}\n')
    return 5


class BaseModel(pydantic.BaseModel):
    if PYDANTIC_V2:
        model_config: ClassVar[ConfigDict] = ConfigDict(
            arbitrary_types_allowed=True,
            ignored_types=(cached_property,),
        )
    else:

        class Config(BaseConfig):
            arbitrary_types_allowed: bool = True
            json_encoders: Dict[Type[Any], Any] = {
                Path: _pathlib_serializer,
                machinery.ModuleSpec: _module_spec_serializer,
            }
            keep_untouched: Tuple[Type[Any], ...] = (cached_property,)


class InterfaceChoices(str, enum.Enum):
    sync = 'sync'
    asyncio = 'asyncio'


class EngineType(str, enum.Enum):
    binary = 'binary'
    library = 'library'
    dataproxy = 'dataproxy'

    @override
    def __str__(self) -> str:
        return self.value


class Module(BaseModel):
    if TYPE_CHECKING:
        spec: machinery.ModuleSpec
    else:
        if PYDANTIC_V2:
            spec: Annotated[
                machinery.ModuleSpec,
                PlainSerializer(lambda x: _module_spec_serializer(x), return_type=str),
            ]
        else:
            spec: machinery.ModuleSpec

    if PYDANTIC_V2:
        model_config: ClassVar[ConfigDict] = ConfigDict(arbitrary_types_allowed=True)
    else:

        class Config(BaseModel.Config):
            arbitrary_types_allowed: bool = True

    # for some reason this is needed in Pydantic v2
    @root_validator(pre=True, skip_on_failure=True)
    @classmethod
    def partial_type_generator_converter(cls, values: object) -> Any:
        if isinstance(values, str):
            return {'spec': values}
        return values

    @field_validator('spec', pre=True, allow_reuse=True)
    @classmethod
    def spec_validator(cls, value: Optional[str]) -> machinery.ModuleSpec:
        spec: Optional[machinery.ModuleSpec] = None

        # TODO: this should really work based off of the schema path
        # and this should suport checking  just partial_types.py if we are in a `prisma` dir
        if value is None:
            value = 'prisma/partial_types.py'

        path = Path.cwd().joinpath(value)
        if path.exists():
            spec = importlib_util.spec_from_file_location('prisma.partial_type_generator', value)
        elif value.startswith('.'):
            raise ValueError(f'No file found at {value} and relative imports are not allowed.')
        else:
            try:
                spec = importlib_util.find_spec(value)
            except ModuleNotFoundError:
                spec = None

        if spec is None:
            raise ValueError(f'Could not find a python file or module at {value}')

        return spec

    def run(self) -> None:
        importlib.invalidate_caches()
        mod = importlib_util.module_from_spec(self.spec)
        loader = self.spec.loader
        assert loader is not None, 'Expected an import loader to exist.'
        assert isinstance(loader, InspectLoader), f'Cannot execute module from loader type: {type(loader)}'

        try:
            loader.exec_module(mod)
        except Exception as exc:
            raise PartialTypeGeneratorError() from exc


class GenericData(GenericModel, Generic[ConfigT]):
    """Root model for the data that prisma provides to the generator.

    WARNING: only one instance of this class may exist at any given time and
    instances should only be constructed using the Data.parse_obj() method
    """

    datamodel: str
    version: str
    generator: 'Generator[ConfigT]'
    dmmf: 'DMMF' = FieldInfo(alias='dmmf')
    schema_path: Path = FieldInfo(alias='schemaPath')
    datasources: List['Datasource'] = FieldInfo(alias='datasources')
    other_generators: List['Generator[_ModelAllowAll]'] = FieldInfo(alias='otherGenerators')
    binary_paths: 'BinaryPaths' = FieldInfo(alias='binaryPaths', default_factory=lambda: BinaryPaths())

    if PYDANTIC_V2:

        @root_validator(pre=False)
        def _set_ctx(self: _ModelT) -> _ModelT:
            data_ctx.set(cast('GenericData[ConfigT]', self))
            return self

    else:

        @classmethod
        @override
        def parse_obj(cls, obj: Any) -> 'GenericData[ConfigT]':
            data = super().parse_obj(obj)  # pyright: ignore[reportDeprecated]
            data_ctx.set(data)
            return data

    def to_params(self) -> Dict[str, Any]:
        """Get the parameters that should be sent to Jinja templates"""
        params = vars(self)
        params['type_schema'] = Schema.from_data(self)
        params['client_types'] = ClientTypes.from_data(self)
        params['max_relation_depth'] = max_relation_chain_depth(self.dmmf.datamodel.models)

        # Add config fields to params (including minimal_runtime)
        config_vars = vars(self.generator.config)
        for key, value in config_vars.items():
            if key not in params:  # Don't override existing params
                params[key] = value

        # add utility functions
        for func in [
            sql_param,
            raise_err,
            type_as_string,
            get_list_types,
            clean_multiline,
            format_documentation,
            model_dict,
            as_literal,
            build_schema_metadata,
            build_enum_metadata,
        ]:
            params[func.__name__] = func

        return params

    @root_validator(pre=True, allow_reuse=True, skip_on_failure=True)
    @classmethod
    def validate_version(cls, values: Dict[Any, Any]) -> Dict[Any, Any]:
        # TODO: test this
        version = values.get('version')
        if not DEBUG_GENERATOR and version != config.expected_engine_version:
            raise ValueError(
                f'Prisma Client Python expected Prisma version: {config.expected_engine_version} '
                f'but got: {version}\n'
                '  If this is intentional, set the PRISMA_PY_DEBUG_GENERATOR environment '
                'variable to 1 and try again.\n'
                f'  If you are using the Node CLI then you must switch to v{config.prisma_version}, e.g. '
                f'npx prisma@{config.prisma_version} generate\n'
                '  or generate the client using the Python CLI, e.g. python3 -m prisma generate'
            )
        return values


class BinaryPaths(BaseModel):
    """This class represents the paths to engine binaries.

    Each property in this class is a mapping of platform name to absolute path, for example:

    ```py
    # This is what will be set on an M1 chip if there are no other `binaryTargets` set
    binary_paths.query_engine == {
        'darwin-arm64': '/Users/robert/.cache/prisma-python/binaries/3.13.0/efdf9b1183dddfd4258cd181a72125755215ab7b/node_modules/prisma/query-engine-darwin-arm64'
    }
    ```

    This is only available if the generator explicitly requests them using the `requires_engines` manifest property.
    """

    query_engine: Dict[str, str] = FieldInfo(
        default_factory=dict,
        alias='queryEngine',
    )
    introspection_engine: Dict[str, str] = FieldInfo(
        default_factory=dict,
        alias='introspectionEngine',
    )
    migration_engine: Dict[str, str] = FieldInfo(
        default_factory=dict,
        alias='migrationEngine',
    )
    libquery_engine: Dict[str, str] = FieldInfo(
        default_factory=dict,
        alias='libqueryEngine',
    )
    prisma_format: Dict[str, str] = FieldInfo(
        default_factory=dict,
        alias='prismaFmt',
    )

    if PYDANTIC_V2:
        model_config: ClassVar[ConfigDict] = ConfigDict(extra='allow')
    else:

        class Config(BaseModel.Config):  # pyright: ignore[reportDeprecated]
            extra: Any = (
                pydantic.Extra.allow  # pyright: ignore[reportDeprecated]
            )


class Datasource(BaseModel):
    # TODO: provider enums
    name: str
    provider: str
    active_provider: str = FieldInfo(alias='activeProvider')
    url: 'OptionalValueFromEnvVar'

    # `schemas = [...]` from the multiSchema preview feature
    schemas: List[str] = FieldInfo(default_factory=list)

    source_file_path: Optional[Path] = FieldInfo(alias='sourceFilePath')


class Generator(GenericModel, Generic[ConfigT]):
    name: str
    output: 'ValueFromEnvVar'
    provider: 'OptionalValueFromEnvVar'
    config: ConfigT
    binary_targets: List['ValueFromEnvVar'] = FieldInfo(alias='binaryTargets')
    preview_features: List[str] = FieldInfo(alias='previewFeatures')

    @field_validator('binary_targets')
    @classmethod
    def warn_binary_targets(cls, targets: List['ValueFromEnvVar']) -> List['ValueFromEnvVar']:
        # Prisma by default sends one binary target which is the current platform.
        if len(targets) > 1:
            click.echo(
                click.style(
                    'Warning: ' + 'The binaryTargets option is not officially supported by Prisma Client Python.',
                    fg='yellow',
                ),
                file=sys.stdout,
            )

        return targets

    def has_preview_feature(self, feature: str) -> bool:
        return feature in self.preview_features


class ValueFromEnvVar(BaseModel):
    value: str
    from_env_var: Optional[str] = FieldInfo(alias='fromEnvVar')


class OptionalValueFromEnvVar(BaseModel):
    value: Optional[str] = None
    from_env_var: Optional[str] = FieldInfo(alias='fromEnvVar')

    def resolve(self) -> str:
        value = self.value
        if value is not None:
            return value

        env_var = self.from_env_var
        assert env_var is not None, 'from_env_var should not be None'
        value = os.environ.get(env_var)
        if value is None:
            raise RuntimeError(f'Environment variable not found: {env_var}')

        return value


class Config(BaseSettings):
    """Custom generator config options."""

    interface: InterfaceChoices = FieldInfo(default=InterfaceChoices.asyncio, env='PRISMA_PY_CONFIG_INTERFACE')
    partial_type_generator: Optional[Module] = FieldInfo(default=None, env='PRISMA_PY_CONFIG_PARTIAL_TYPE_GENERATOR')
    recursive_type_depth: int = FieldInfo(
        default_factory=_recursive_type_depth_factory,
        env='PRISMA_PY_CONFIG_RECURSIVE_TYPE_DEPTH',
    )
    engine_type: EngineType = FieldInfo(default=EngineType.binary, env='PRISMA_PY_CONFIG_ENGINE_TYPE')

    # this should be a list of experimental features
    # https://github.com/prisma/prisma/issues/12442
    enable_experimental_decimal: bool = FieldInfo(default=False, env='PRISMA_PY_CONFIG_ENABLE_EXPERIMENTAL_DECIMAL')

    minimal_runtime: bool = FieldInfo(
        default=False,
        env='PRISMA_PY_CONFIG_MINIMAL_RUNTIME',
        alias='minimalRuntime',
    )
    separate_model_files: bool = FieldInfo(
        default=False,
        env='PRISMA_PY_CONFIG_SEPARATE_MODEL_FILES',
        alias='separateModelFiles',
    )
    scalar_fields_only: bool = FieldInfo(
        default=False,
        env='PRISMA_PY_CONFIG_SCALAR_FIELDS_ONLY',
        alias='scalarFieldsOnly',
        description='Skip relationship fields in models to avoid circular validation and reduce memory',
    )
    recursive_validation_models: bool = FieldInfo(
        default=False,
        env='PRISMA_PY_CONFIG_RECURSIVE_VALIDATION_MODELS',
        alias='recursiveValidationModels',
        description=(
            'Generate true-recursive, lazily-built (defer_build) Pydantic v2 models so '
            'large/deep schemas keep full runtime validation without exploding memory or '
            'crashing on import. Requires Pydantic v2.'
        ),
    )
    lazy_actions: bool = FieldInfo(
        default=False,
        env='PRISMA_PY_CONFIG_LAZY_ACTIONS',
        alias='lazyActions',
        description=(
            'Create per-model action namespaces (client.user, client.post, ...) on first '
            'attribute access instead of eagerly in Prisma.__init__, and defer importing '
            'the actions module until first database access. Client construction and '
            'import become O(models touched) instead of O(models in schema).'
        ),
    )
    schema_metadata: bool = FieldInfo(
        default=False,
        env='PRISMA_PY_CONFIG_SCHEMA_METADATA',
        alias='schemaMetadata',
        description=(
            'Emit the physical database schema (tables, columns, primary keys, unique '
            'constraints, indexes, enum value mappings and fully resolved foreign keys) '
            'into the generated metadata module. The binary query engine derives all of '
            'this itself, so it is dead weight there; a client that speaks SQL directly '
            'cannot build a single query without it.'
        ),
    )
    model_backend: str = FieldInfo(
        default='pydantic',
        env='PRISMA_PY_CONFIG_MODEL_BACKEND',
        alias='modelBackend',
        description=(
            'Record model backend. "pydantic" (default) generates the usual pydantic '
            'BaseModel records. "slim" generates pydantic-free __slots__ records with '
            'compiled converters (requires separateModelFiles). "msgspec" generates '
            'msgspec.Struct records decoded in C — the fastest backend (requires the '
            'msgspec package, incompatible with separateModelFiles). Both alternatives '
            'convert trusted engine data rather than validating arbitrary input.'
        ),
    )

    # this seems to be the only good method for setting the contextvar as
    # we don't control the actual construction of the object like we do for
    # the Data model.
    # we do not expose this to type checkers so that the generated __init__
    # signature is preserved.
    if not TYPE_CHECKING:

        def __init__(self, **kwargs: object) -> None:
            super().__init__(**kwargs)
            config_ctx.set(self)

    if PYDANTIC_V2:
        model_config: ClassVar[ConfigDict] = ConfigDict(
            extra='forbid',
            use_enum_values=True,
            populate_by_name=True,
        )
    else:
        if not TYPE_CHECKING:

            class Config(BaseSettingsConfig):
                extra: pydantic.Extra = pydantic.Extra.forbid
                use_enum_values: bool = True
                env_prefix: str = 'prisma_py_config_'
                allow_population_by_field_name: bool = True

                @classmethod
                def customise_sources(cls, init_settings, env_settings, file_secret_settings):
                    # prioritise env settings over init settings
                    return env_settings, init_settings, file_secret_settings

    @root_validator(pre=True, skip_on_failure=True)
    @classmethod
    def transform_engine_type(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        # prioritise env variable over schema option
        engine_type = os.environ.get('PRISMA_CLIENT_ENGINE_TYPE')
        if engine_type is None:
            engine_type = values.get('engineType')

        # only add engine_type if it is present
        if engine_type is not None:
            values['engine_type'] = engine_type
            values.pop('engineType', None)

        return values

    @root_validator(pre=True, skip_on_failure=True)
    @classmethod
    def transform_minimal_runtime(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        # Handle camelCase from schema
        minimal_runtime = values.get('minimalRuntime')
        if minimal_runtime is not None:
            values['minimal_runtime'] = minimal_runtime
            values.pop('minimalRuntime', None)

        return values

    @root_validator(pre=True, skip_on_failure=True)
    @classmethod
    def transform_separate_model_files(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        # Handle camelCase from schema
        separate_model_files = values.get('separateModelFiles')
        if separate_model_files is not None:
            values['separate_model_files'] = separate_model_files
            values.pop('separateModelFiles', None)

        return values

    @root_validator(pre=True, skip_on_failure=True)
    @classmethod
    def transform_scalar_fields_only(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        # Handle camelCase from schema
        scalar_fields_only = values.get('scalarFieldsOnly')
        if scalar_fields_only is not None:
            values['scalar_fields_only'] = scalar_fields_only
            values.pop('scalarFieldsOnly', None)

        return values

    @root_validator(pre=True, skip_on_failure=True)
    @classmethod
    def transform_recursive_validation_models(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        # Handle camelCase from schema
        recursive_validation_models = values.get('recursiveValidationModels')
        if recursive_validation_models is not None:
            values['recursive_validation_models'] = recursive_validation_models
            values.pop('recursiveValidationModels', None)

        return values

    @root_validator(pre=True, skip_on_failure=True)
    @classmethod
    def transform_lazy_actions(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        # Handle camelCase from schema
        lazy_actions = values.get('lazyActions')
        if lazy_actions is not None:
            values['lazy_actions'] = lazy_actions
            values.pop('lazyActions', None)

        return values

    @root_validator(pre=True, skip_on_failure=True)
    @classmethod
    def transform_model_backend(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        # Handle camelCase from schema
        model_backend = values.get('modelBackend')
        if model_backend is not None:
            values['model_backend'] = model_backend
            values.pop('modelBackend', None)

        backend = values.get('model_backend')
        if backend is not None and backend not in ('pydantic', 'slim', 'msgspec'):
            raise ValueError(f'modelBackend must be "pydantic", "slim" or "msgspec", got: {backend!r}')

        return values

    @root_validator(pre=True, skip_on_failure=True)
    @classmethod
    def removed_http_option_validator(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        http = values.get('http')
        if http is not None:
            if http in {'aiohttp', 'httpx-async'}:
                option = 'asyncio'
            elif http in {'requests', 'httpx-sync'}:
                option = 'sync'
            else:  # pragma: no cover
                # invalid http option, let pydantic handle the error
                return values

            raise ValueError(
                'The http option has been removed in favour of the interface option.\n'
                '  Please remove the http option from your Prisma schema and replace it with:\n'
                f'  interface = "{option}"'
            )
        return values

    if PYDANTIC_V2:

        @root_validator(pre=True, skip_on_failure=True)
        @classmethod
        def partial_type_generator_converter(cls, values: Dict[str, Any]) -> Dict[str, Any]:
            # ensure env resolving happens
            values = cast(Dict[str, Any], cls.root_validator(values))  # type: ignore

            value = values.get('partial_type_generator')

            try:
                values['partial_type_generator'] = Module(
                    spec=value  # pyright: ignore[reportArgumentType]
                )
            except ValueError:
                if value is None:
                    # no config value passed and the default location was not found
                    return values
                raise

            return values

    else:

        @field_validator('partial_type_generator', pre=True, always=True, allow_reuse=True)
        @classmethod
        def _partial_type_generator_converter(cls, value: Optional[str]) -> Optional[Module]:
            try:
                return Module(
                    spec=value  # pyright: ignore[reportArgumentType]
                )
            except ValueError:
                if value is None:
                    # no config value passed and the default location was not found
                    return None
                raise

    @field_validator('recursive_type_depth', always=True, allow_reuse=True)
    @classmethod
    def recursive_type_depth_validator(cls, value: int) -> int:
        if value < -1 or value in {0, 1}:
            raise ValueError('Value must equal -1 or be greater than 1.')
        return value

    @field_validator('engine_type', always=True, allow_reuse=True)
    @classmethod
    def engine_type_validator(cls, value: EngineType) -> EngineType:
        if value == EngineType.binary:
            return value
        elif value == EngineType.dataproxy:  # pragma: no cover
            raise ValueError('Prisma Client Python does not support the Prisma Data Proxy yet.')
        elif value == EngineType.library:  # pragma: no cover
            raise ValueError('Prisma Client Python does not support native engine bindings yet.')
        else:  # pragma: no cover
            assert_never(value)


class DMMFEnumType(BaseModel):
    name: str
    values: List[object]


class DMMFEnumTypes(BaseModel):
    prisma: List[DMMFEnumType]


class PrismaSchema(BaseModel):
    enum_types: DMMFEnumTypes = FieldInfo(alias='enumTypes')


class DMMF(BaseModel):
    datamodel: 'Datamodel'
    prisma_schema: PrismaSchema = FieldInfo(alias='schema')


class IndexField(BaseModel):
    """One column within an index, with its per-column modifiers."""

    name: str
    sort_order: Optional[str] = FieldInfo(alias='sortOrder', default=None)
    length: Optional[int] = None
    operator_class: Optional[str] = FieldInfo(alias='operatorClass', default=None)


class Index(BaseModel):
    """An entry from `datamodel.indexes`.

    This is the *only* place Prisma reports `@@index`, index `map:`/`sort:`/
    `length:`/`type:` modifiers, and the resolved database-level name of a
    unique or primary key constraint. It is sent on the wire for every schema;
    it was previously discarded.
    """

    model: str
    # 'id' | 'unique' | 'normal' | 'fulltext'
    type: str
    is_defined_on_field: bool = FieldInfo(alias='isDefinedOnField')
    name: Optional[str] = None
    db_name: Optional[str] = FieldInfo(alias='dbName', default=None)
    algorithm: Optional[str] = None
    clustered: Optional[bool] = None
    fields: List['IndexField'] = FieldInfo(default_factory=list)


class Datamodel(BaseModel):
    enums: List['Enum']
    models: List['Model']

    # `@@index` / `@@fulltext` / resolved constraint names live here and nowhere
    # else in the DMMF
    indexes: List['Index'] = FieldInfo(default_factory=list)

    # not implemented yet
    types: List[object]

    def indexes_for(self, model: str) -> List['Index']:
        """`@@index`/`@@unique`/`@@id` entries declared on the given model."""
        return [index for index in self.indexes if index.model == model]

    @field_validator('types')
    @classmethod
    def no_composite_types_validator(cls, types: List[object]) -> object:
        if types:
            raise ValueError(
                'Composite types are not supported yet. Please indicate you need this here: https://github.com/RobertCraigie/prisma-client-py/issues/314'
            )

        return types


class Enum(BaseModel):
    name: str
    db_name: Optional[str] = FieldInfo(alias='dbName')
    values: List['EnumValue']


class EnumValue(BaseModel):
    name: str
    db_name: Optional[str] = FieldInfo(alias='dbName')


class ModelExtension(BaseModel):
    instance_name: Optional[str] = None

    @field_validator('instance_name')
    @classmethod
    def instance_name_validator(cls, name: Optional[str]) -> Optional[str]:
        if not name:
            return name

        if not name.isidentifier():
            raise ValueError(f'Custom Model instance_name "{name}" is not a valid Python identifier')

        return name


class Model(BaseModel):
    name: str
    documentation: Optional[str] = None
    db_name: Optional[str] = FieldInfo(alias='dbName')
    is_generated: bool = FieldInfo(alias='isGenerated')
    compound_primary_key: Optional['PrimaryKey'] = FieldInfo(alias='primaryKey')
    unique_indexes: List['UniqueIndex'] = FieldInfo(alias='uniqueIndexes')
    # the legacy `List[List[str]]` form of the same information; it is the only
    # place field *order* within a compound unique is guaranteed
    unique_fields: List[List[str]] = FieldInfo(alias='uniqueFields', default_factory=list)
    all_fields: List['Field'] = FieldInfo(alias='fields')

    # stores the parsed DSL, not an actual field defined by prisma
    extension: Optional[ModelExtension] = None

    _sampler: Sampler = PrivateAttr()

    def __init__(self, **data: Any) -> None:
        super().__init__(**data)
        self._sampler = Sampler(self)

    @root_validator(pre=True, allow_reuse=True)
    @classmethod
    def validate_dsl_extension(cls, values: Dict[Any, Any]) -> Dict[Any, Any]:
        documentation = values.get('documentation')
        if not documentation:
            return values

        parsed = parse_schema_dsl(documentation)
        if parsed['type'] == 'invalid':
            raise ValueError(parsed['error'])

        if parsed['type'] == 'ok':
            values['extension'] = model_parse(ModelExtension, parsed['value']['arguments'])

        return values

    @field_validator('name')
    @classmethod
    def name_validator(cls, name: str) -> str:
        if iskeyword(name):
            raise ValueError(
                f'Model name "{name}" shadows a Python keyword; '
                f'use a different model name with \'@@map("{name}")\'.'
            )

        if iskeyword(name.lower()):
            raise ValueError(
                f'Model name "{name}" results in a client property that shadows a Python keyword; '
                f'use a different model name with \'@@map("{name}")\'.'
            )

        return name

    @property
    def related_models(self) -> Iterator['Model']:
        models = get_datamodel().models
        for field in self.relational_fields:
            for model in models:
                if field.type == model.name:
                    yield model

    @property
    def relational_fields(self) -> Iterator['Field']:
        for field in self.all_fields:
            if field.is_relational:
                yield field

    @property
    def scalar_fields(self) -> Iterator['Field']:
        for field in self.all_fields:
            if not field.is_relational:
                yield field

    @property
    def atomic_fields(self) -> Iterator['Field']:
        for field in self.all_fields:
            if field.type in ATOMIC_FIELD_TYPES:
                yield field

    @property
    def required_array_fields(self) -> Iterator['Field']:
        for field in self.all_fields:
            if field.is_list and not field.relation_name and field.is_required:
                yield field

    # TODO: support combined unique constraints
    @cached_property
    def id_field(self) -> Optional['Field']:
        """Find a field that can be passed to the model's `WhereUnique` filter"""
        for field in self.scalar_fields:  # pragma: no branch
            if field.is_id or field.is_unique:
                return field
        return None

    @property
    def has_relational_fields(self) -> bool:
        try:
            next(self.relational_fields)
        except StopIteration:
            return False
        else:
            return True

    @property
    def instance_name(self) -> str:
        """The name of this model in the generated client class, e.g.

        `User` -> `Prisma().user`
        """
        if self.extension and self.extension.instance_name:
            return self.extension.instance_name

        return self.name.lower()

    @property
    def plural_name(self) -> str:
        name = self.instance_name
        if name.endswith('s'):
            return name
        return f'{name}s'

    def resolve_field(self, name: str) -> 'Field':
        for field in self.all_fields:
            if field.name == name:
                return field

        raise LookupError(f'Could not find a field with name: {name}')

    def sampler(self) -> Sampler:
        return self._sampler

    # -- physical schema derivation ------------------------------------------
    #
    # Everything below reconstructs the *database* shape of the model from the
    # DMMF. The binary query engine has always done this for us, so none of it
    # was needed before; a client that talks SQL directly needs all of it.

    @property
    def table_name(self) -> str:
        """The physical table name, honouring `@@map`."""
        return self.db_name or self.name

    @cached_property
    def primary_key_fields(self) -> List[str]:
        """Field names forming the primary key, in declaration order.

        Empty for the (legal, if unusual) model that has no `@id`/`@@id` and is
        identified only by a unique constraint.
        """
        if self.compound_primary_key is not None:
            return list(self.compound_primary_key.fields)

        for field in self.all_fields:
            if field.is_id:
                return [field.name]

        return []

    def get_related_field(self, field: 'Field') -> Optional['Field']:
        """The field on the other side of `field`'s relation.

        Both sides of a Prisma relation carry the same `relationName`, which is
        what makes this resolvable. For a self-relation both sides live on this
        same model, so identity — not name — is what excludes `field` itself.
        """
        related = field.get_relational_model()
        if related is None:
            return None

        for candidate in related.all_fields:
            if candidate.relation_name != field.relation_name:
                continue
            if candidate is field:
                continue
            return candidate

        return None

    def relation_metadata(self, field: 'Field') -> Dict[str, Any]:
        """Everything a SQL compiler needs to join, filter or write `field`.

        The shapes, named to match `docs/sqlalchemy-refactor/`:

        - `to-one-owner`   this model holds the foreign key
        - `to-one-inverse` the *other* model holds the foreign key
        - `to-many`        the other model holds the foreign key, many rows
        - `many-to-many`   an implicit join table Prisma manages invisibly

        The distinction that actually bites is `fk_required`: when the foreign
        key columns are `NOT NULL` there is no way to express `disconnect` or a
        `set` that drops rows, and the engine answers those with P2014. Getting
        this wrong produces a client that silently orphans rows instead of
        raising, which is why it is derived here rather than guessed later.
        """
        related_model = field.get_relational_model()
        assert related_model is not None, f'{self.name}.{field.name} is not relational'

        back = self.get_related_field(field)

        # Prisma reports the foreign key on the owning side only; the inverse
        # side gets empty lists. Exactly one side of a to-one/to-many relation
        # owns it, and neither side owns an implicit m2m.
        owner = bool(field.relation_from_fields)

        if owner:
            fk_model, fk_field = self, field
        elif back is not None and back.relation_from_fields:
            fk_model, fk_field = related_model, back
        else:
            fk_model, fk_field = None, None

        if field.is_list and back is not None and back.is_list:
            shape = 'many-to-many'
        elif field.is_list:
            shape = 'to-many'
        elif owner:
            shape = 'to-one-owner'
        else:
            shape = 'to-one-inverse'

        fk_fields: List[str] = []
        fk_columns: List[str] = []
        referenced_fields: List[str] = []
        referenced_columns: List[str] = []
        fk_required = False

        if fk_model is not None and fk_field is not None:
            fk_fields = list(fk_field.relation_from_fields or [])
            referenced_fields = list(fk_field.relation_to_fields or [])

            resolved = [fk_model.resolve_field(name) for name in fk_fields]
            fk_columns = [f.column_name for f in resolved]
            # A relation is mandatory only if *every* foreign key column is
            # NOT NULL; a partially-optional compound FK can still be nulled.
            fk_required = bool(resolved) and all(f.is_required for f in resolved)

            target = fk_field.get_relational_model()
            assert target is not None
            referenced_columns = [target.resolve_field(name).column_name for name in referenced_fields]

        meta: Dict[str, Any] = {
            'to': related_model.name,
            'shape': shape,
            'relation_name': field.relation_name,
            'is_list': field.is_list,
            'nullable': not field.is_required,
            'owner': owner,
            'back_field': back.name if back is not None else None,
            'fk_model': fk_model.name if fk_model is not None else None,
            'fk_fields': fk_fields,
            'fk_columns': fk_columns,
            'referenced_fields': referenced_fields,
            'referenced_columns': referenced_columns,
            'fk_required': fk_required,
            # None means "Prisma's default for this arity" — not "no action".
            'on_delete': (fk_field.relation_on_delete if fk_field is not None else None),
            # Always present, including on the relations where it is trivially
            # False. The runbook tells callers to check this before traversing a
            # relation, and a key that exists on only 2 of 640 relations makes
            # that instruction a KeyError.
            'join_ambiguous': False,
        }

        if shape == 'many-to-many':
            meta.update(self._implicit_join_metadata(field, related_model))

        return meta

    def _implicit_join_metadata(self, field: 'Field', related_model: 'Model') -> Dict[str, Any]:
        """Locate the join table Prisma creates for an implicit m2m relation.

        The table is `_<relationName>` with columns `A` and `B`, where `A`
        belongs to whichever model sorts first by name. For a self-relation both
        sides sort equally and the assignment depends on declaration order in a
        way the DMMF does not expose — so we refuse to guess and say so, rather
        than emitting a coin-flip a compiler would silently trust.
        """
        assert field.relation_name is not None
        table = f'_{field.relation_name}'

        if related_model.name == self.name:
            return {
                'join_table': table,
                'join_self_column': None,
                'join_other_column': None,
                'join_ambiguous': True,
            }

        self_first = self.name < related_model.name
        return {
            'join_table': table,
            'join_self_column': 'A' if self_first else 'B',
            'join_other_column': 'B' if self_first else 'A',
            'join_ambiguous': False,
        }


class Constraint(BaseModel):
    name: str
    fields: List[str]

    @root_validator(pre=True, allow_reuse=True, skip_on_failure=True)
    @classmethod
    def resolve_name(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        name = values.get('name')
        if isinstance(name, str):
            return values

        values['name'] = '_'.join(values['fields'])
        return values


class PrimaryKey(Constraint):
    pass


class UniqueIndex(Constraint):
    pass


class Field(BaseModel):
    name: str
    documentation: Optional[str] = None

    # TODO: switch to enums
    kind: str
    type: str

    # `@map("col")` — the physical column name. Prisma omits this key entirely
    # when the field is not mapped, hence Optional with a None default.
    db_name: Optional[str] = FieldInfo(alias='dbName', default=None)

    # `@db.VarChar(255)` -> ('VarChar', ['255']).
    #
    # NOTE: verified empirically that Prisma does *not* send this on the wire for
    # the current generator protocol — there are zero `nativeType` keys in a real
    # payload even for a schema using `@db.VarChar`/`@db.Decimal`. It is only
    # recoverable by lexing the raw schema text, which `GenericData.datamodel`
    # does carry. The field is modelled here so that a future Prisma release
    # sending it is picked up automatically (and so the completeness test would
    # notice), but consumers must not assume it is populated.
    native_type: Optional[Tuple[str, List[str]]] = FieldInfo(alias='nativeType', default=None)

    is_id: bool = FieldInfo(alias='isId')
    is_list: bool = FieldInfo(alias='isList')
    is_unique: bool = FieldInfo(alias='isUnique')
    is_required: bool = FieldInfo(alias='isRequired')
    is_read_only: bool = FieldInfo(alias='isReadOnly')
    is_generated: bool = FieldInfo(alias='isGenerated')
    is_updated_at: bool = FieldInfo(alias='isUpdatedAt')

    default: Optional[Union['DefaultValue', object, List[object]]] = None
    has_default_value: bool = FieldInfo(alias='hasDefaultValue')

    relation_name: Optional[str] = FieldInfo(alias='relationName', default=None)
    relation_on_delete: Optional[str] = FieldInfo(alias='relationOnDelete', default=None)
    relation_to_fields: Optional[List[str]] = FieldInfo(
        alias='relationToFields',
        default=None,
    )
    relation_from_fields: Optional[List[str]] = FieldInfo(
        alias='relationFromFields',
        default=None,
    )

    _last_sampled: Optional[str] = PrivateAttr()

    @root_validator(pre=True, skip_on_failure=True)
    @classmethod
    def scalar_type_validator(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        kind = values.get('kind')
        type_ = values.get('type')

        if kind == 'scalar':
            if type_ is not None and type_ not in TYPE_MAPPING:
                raise ValueError(f'Unsupported scalar field type: {type_}')

        return values

    @field_validator('type')
    @classmethod
    def experimental_decimal_validator(cls, typ: str) -> str:
        if typ == 'Decimal':
            config = get_config()

            # skip validating the experimental flag if we are
            # being called from a custom generator
            if isinstance(config, Config) and not config.enable_experimental_decimal:
                raise ValueError(
                    'Support for the Decimal type is experimental\n'
                    '  As such you must set the `enable_experimental_decimal` config flag to true\n'
                    '  for more information see: https://github.com/RobertCraigie/prisma-client-py/issues/106'
                )

        return typ

    @field_validator('name')
    @classmethod
    def name_validator(cls, name: str) -> str:
        if getattr(BaseModel, name, None):
            raise ValueError(
                f'Field name "{name}" shadows a BaseModel attribute; '
                f'use a different field name with \'@map("{name}")\'.'
            )

        if iskeyword(name):
            raise ValueError(
                f'Field name "{name}" shadows a Python keyword; ' f'use a different field name with \'@map("{name}")\'.'
            )

        if name == 'prisma':
            raise ValueError(
                'Field name "prisma" shadows a Prisma Client Python method; '
                'use a different field name with \'@map("prisma")\'.'
            )

        if name in QUERY_BUILDER_ALIASES:
            raise ValueError(
                f'Field name "{name}" shadows an internal keyword; '
                f'use a different field name with \'@map("{name}")\''
            )

        return name

    # TODO: cache the properties
    @property
    def python_type(self) -> str:
        type_ = self._actual_python_type
        if self.is_list:
            return f'List[{type_}]'
        return type_

    _SLIM_SCALAR_TAGS: ClassVar[Dict[str, str]] = {
        'String': 'str',
        'Int': 'int',
        'BigInt': 'bigint',
        'Float': 'float',
        'Boolean': 'bool',
        'DateTime': 'datetime',
        'Json': 'json',
        'Bytes': 'base64',
        'Decimal': 'decimal',
    }

    def slim_spec(self) -> str:
        """Conversion spec literal for the slim model backend.

        Nested tuples consumed by `prisma._fastparse.converter_for_spec`,
        e.g. `('opt', ('list', ('model', 'Post')))`.
        """
        spec: object
        if self.kind == 'object':
            spec = ('model', self.type)
        elif self.kind == 'enum':
            spec = 'enum'
        else:
            spec = self._SLIM_SCALAR_TAGS[self.type]

        if self.is_list:
            spec = ('list', spec)
        if not self.is_required or self.relation_name is not None:
            spec = ('opt', spec)
        return repr(spec)

    @property
    def python_type_as_string(self) -> str:
        type_ = self._actual_python_type
        if self.is_list:
            type_ = type_.replace("'", "\\'")
            return f"'List[{type_}]'"

        if not type_.startswith("'"):
            type_ = f"'{type_}'"

        return type_

    @property
    def _actual_python_type(self) -> str:
        if self.kind == 'enum':
            return f"'enums.{self.type}'"

        if self.kind == 'object':
            return f"'models.{self.type}'"

        try:
            return TYPE_MAPPING[self.type]
        except KeyError as exc:
            # TODO: handle this better
            raise RuntimeError(
                f'Could not parse {self.name} due to unknown type: {self.type}',
            ) from exc

    @property
    def create_input_type(self) -> str:
        if self.kind != 'object':
            return self.python_type

        if self.is_list:
            return f"'{self.type}CreateManyNestedWithoutRelationsInput'"

        return f"'{self.type}CreateNestedWithoutRelationsInput'"

    @property
    def where_input_type(self) -> str:
        typ = self.type
        if self.is_relational:
            if self.is_list:
                return f"'{typ}ListRelationFilter'"
            return f"'{typ}RelationFilter'"

        if self.is_list:
            self.check_supported_scalar_list_type()
            return f"'types.{typ}ListFilter'"

        if typ in FILTER_TYPES:
            if self.is_optional:
                return f"Union[None, {self._actual_python_type}, 'types.{typ}Filter']"
            return f"Union[{self._actual_python_type}, 'types.{typ}Filter']"

        return self.python_type

    @property
    def where_aggregates_input_type(self) -> str:
        if self.is_relational:  # pragma: no cover
            raise RuntimeError('This type is not valid for relational fields')

        typ = self.type
        if typ in FILTER_TYPES:
            return f"Union[{self._actual_python_type}, 'types.{typ}WithAggregatesFilter']"
        return self.python_type

    @property
    def relational_args_type(self) -> str:
        if self.is_list:
            return f'FindMany{self.type}Args'
        return f'{self.type}Args'

    @property
    def required_on_create(self) -> bool:
        return (
            self.is_required
            and not self.is_updated_at
            and not self.has_default_value
            and not self.relation_name
            and not self.is_list
        )

    @property
    def is_optional(self) -> bool:
        return not (self.is_required and not self.relation_name)

    @property
    def is_relational(self) -> bool:
        return self.relation_name is not None

    @property
    def column_name(self) -> str:
        """The physical column name, honouring `@map`."""
        return self.db_name or self.name

    @property
    def default_spec(self) -> Optional[Dict[str, Any]]:
        """The default, split into the two cases a SQL compiler must treat
        differently.

        A *generator* (`cuid()`, `uuid()`, `now()`, `autoincrement()`, ...) has
        to be evaluated per-row, either client-side or by the database. A
        *literal* is a constant that can be inlined. Prisma sends the first as
        an object and the second as a bare JSON value, which is easy to conflate
        — a `String` field defaulting to the literal `"uuid"` is not a uuid
        generator.
        """
        if not self.has_default_value:
            return None

        default = self.default
        if isinstance(default, DefaultValue):
            args = default.args
            return {
                'kind': 'generator',
                # Prisma normalises `@default(uuid())` to `uuid(4)` on the wire —
                # the schema text says one thing and the DMMF another. Consumers
                # match on the generator, not the version, so strip it. Leaving
                # it on made every `uuid()` schema fail to build at all.
                'name': default.name.split('(', 1)[0],
                'version': default.name[len(default.name.split('(', 1)[0]) :].strip('()') or None,
                'args': args if isinstance(args, list) else ([] if args is None else [args]),
            }

        return {'kind': 'literal', 'value': default}

    @property
    def is_atomic(self) -> bool:
        return self.type in ATOMIC_FIELD_TYPES

    @property
    def is_number(self) -> bool:
        return self.type in {'Int', 'BigInt', 'Float'}

    def maybe_optional(self, typ: str) -> str:
        """Wrap the given type string within `Optional` if applicable"""
        if self.is_required or self.is_relational:
            return typ
        return f'Optional[{typ}]'

    def get_update_input_type(self) -> str:
        if self.kind == 'object':
            if self.is_list:
                return f"'{self.type}UpdateManyWithoutRelationsInput'"
            return f"'{self.type}UpdateOneWithoutRelationsInput'"

        if self.is_list:
            self.check_supported_scalar_list_type()
            return f"'types.{self.type}ListUpdate'"

        if self.is_atomic:
            return f'Union[Atomic{self.type}Input, {self.python_type}]'

        return self.python_type

    def check_supported_scalar_list_type(self) -> None:
        if self.type not in FILTER_TYPES and self.kind != 'enum':  # pragma: no branch
            raise UnsupportedListTypeError(self.type)

    def get_relational_model(self) -> Optional['Model']:
        if not self.is_relational:
            return None

        name = self.type
        for model in get_datamodel().models:
            if model.name == name:
                return model
        return None

    def get_corresponding_enum(self) -> Optional['Enum']:
        typ = self.type
        for enum in get_datamodel().enums:
            if enum.name == typ:
                return enum
        return None  # pragma: no cover

    def get_sample_data(self, *, increment: bool = True) -> str:
        # returning the same data that was last sampled is useful
        # for documenting methods like upsert() where data is duplicated
        if not increment and self._last_sampled is not None:
            return self._last_sampled

        sampled = self._get_sample_data()
        if self.is_list:
            sampled = f'[{sampled}]'

        self._last_sampled = sampled
        return sampled

    def _get_sample_data(self) -> str:
        if self.is_relational:  # pragma: no cover
            raise RuntimeError('Data sampling for relational fields not supported yet')

        if self.kind == 'enum':
            enum = self.get_corresponding_enum()
            assert enum is not None, self.type
            return f'enums.{enum.name}.{FAKER.from_list(enum.values).name}'

        typ = self.type
        if typ == 'Boolean':
            return str(FAKER.boolean())
        elif typ == 'Int':
            return str(FAKER.integer())
        elif typ == 'String':
            return f"'{FAKER.string()}'"
        elif typ == 'Float':
            return f'{FAKER.integer()}.{FAKER.integer() // 10000}'
        elif typ == 'BigInt':  # pragma: no cover
            return str(FAKER.integer() * 12)
        elif typ == 'DateTime':
            # TODO: random dates
            return 'datetime.datetime.utcnow()'
        elif typ == 'Json':
            return f"Json({{'{FAKER.string()}': True}})"
        elif typ == 'Bytes':
            return f"Base64.encode(b'{FAKER.string()}')"
        elif typ == 'Decimal':
            return f"Decimal('{FAKER.integer()}.{FAKER.integer() // 10000}')"
        else:  # pragma: no cover
            raise RuntimeError(f'Sample data not supported for {typ} yet')


class DefaultValue(BaseModel):
    args: Any = None
    name: str


class _EmptyModel(BaseModel):
    if PYDANTIC_V2:
        model_config: ClassVar[ConfigDict] = ConfigDict(extra='forbid')
    elif not TYPE_CHECKING:

        class Config(BaseModel.Config):
            extra: pydantic.Extra = pydantic.Extra.forbid


class _ModelAllowAll(BaseModel):
    if PYDANTIC_V2:
        model_config: ClassVar[ConfigDict] = ConfigDict(extra='allow')
    elif not TYPE_CHECKING:

        class Config(BaseModel.Config):
            extra: pydantic.Extra = pydantic.Extra.allow


class PythonNames(BaseModel):
    def client_class(self, _for_async: bool) -> str:
        return 'Prisma'


class PythonData(GenericData[Config]):
    """Data class including the default Prisma Client Python config"""

    if not PYDANTIC_V2:

        class Config(BaseConfig):
            arbitrary_types_allowed: bool = True
            json_encoders: Dict[Type[Any], Any] = {
                Path: _pathlib_serializer,
                machinery.ModuleSpec: _module_spec_serializer,
            }
            keep_untouched: Tuple[Type[Any], ...] = (cached_property,)

    names: PythonNames = PythonNames()


class DefaultData(GenericData[_EmptyModel]):
    """Data class without any config options"""


# this has to be defined as a type alias instead of a class
# as its purpose is to signify that the data is config agnostic
AnyData = GenericData[Any]

model_rebuild(Enum)
model_rebuild(DMMF)
model_rebuild(GenericData)
model_rebuild(Field)
model_rebuild(Model)
model_rebuild(Datamodel)
model_rebuild(Generator)
model_rebuild(Datasource)


from .errors import (
    TemplateError,
    PartialTypeGeneratorError,
)
from .schema import Schema, ClientTypes
