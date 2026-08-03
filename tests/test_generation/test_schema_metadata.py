"""Tests for the physical schema reconstructed from the DMMF.

The generated client has never known what a table is called — it names models
and fields, and the Rust query engine, which parsed `schema.prisma` itself,
turns those into SQL. A client that emits SQL directly has to reconstruct the
physical schema from the DMMF, and every one of these assertions is something a
generated query gets wrong if the reconstruction is wrong.

The assertions are *absolute*, not differential: they name the exact table,
column and constraint the database has. A differential test against the query
engine would pass on any misreading both sides happen to share.

The fixture is recorded from a real `prisma generate` against
`data/dmmf_wire_sample.prisma`; re-record with
`PRISMA_PY_DEBUG_GENERATOR=1 python -m prisma generate` and copy
`src/prisma/generator/debug-params.json`, keeping only `datasources` and
`dmmf.datamodel`.
"""

from __future__ import annotations

from typing import Any, Dict, Iterator, cast

import pytest

from prisma.generator import models as generator_models
from prisma.generator.models import (
    Datamodel,
    build_enum_metadata,
    build_schema_metadata,
)

from ..dmmf_sample import SAMPLE, schema_text, loaded_datamodel


@pytest.fixture(scope='module', name='datamodel')
def datamodel_fixture() -> Iterator[Datamodel]:
    if not SAMPLE.exists():  # pragma: no cover
        pytest.skip(f'wire sample not recorded at {SAMPLE}')

    with loaded_datamodel() as datamodel:
        yield datamodel


@pytest.fixture(scope='module', name='schema')
def schema_fixture(datamodel: Datamodel) -> Dict[str, Any]:
    return build_schema_metadata(datamodel, schema_text())


# -- naming ------------------------------------------------------------------


def test_table_name_honours_at_at_map(schema: Dict[str, Any]) -> None:
    assert schema['Account']['table'] == 'accounts'
    # unmapped models keep the model name
    assert schema['Entry']['table'] == 'Entry'


def test_column_name_honours_at_map(schema: Dict[str, Any]) -> None:
    assert schema['Account']['fields']['slug']['column'] == 'url_slug'
    assert schema['Account']['fields']['email']['column'] == 'email'


# -- keys and constraints ----------------------------------------------------


def test_single_primary_key(schema: Dict[str, Any]) -> None:
    pk = schema['Account']['primary_key']
    assert pk['fields'] == ['id']
    assert pk['columns'] == ['id']
    # a single `@id` has no Prisma-level constraint name
    assert pk['name'] is None


def test_compound_primary_key_keeps_field_order(schema: Dict[str, Any]) -> None:
    pk = schema['Composite']['primary_key']
    assert pk['fields'] == ['left', 'right']
    assert pk['columns'] == ['left', 'right']
    assert pk['name'] == 'left_right'


def test_primary_key_db_name_is_not_guessed(schema: Dict[str, Any]) -> None:
    """`<table>_pkey` is PostgreSQL's convention, not Prisma's.

    MySQL calls it `PRIMARY`. Deriving one here would be wrong for half the
    supported providers, so an unmapped `@@id` reports None.
    """
    assert schema['Composite']['primary_key']['db_name'] is None


def test_unique_constraint_names_are_two_namespaces(schema: Dict[str, Any]) -> None:
    unique = _unique_named(schema, 'Account', 'slug_role')
    # what `where={'slug_role': ...}` uses
    assert unique['name'] == 'slug_role'
    # what the database calls the constraint — note it uses the *mapped* table
    # and column names, not the Prisma ones
    assert unique['db_name'] == 'accounts_url_slug_role_key'
    assert unique['fields'] == ['slug', 'role']
    assert unique['columns'] == ['url_slug', 'role']
    assert unique['is_defined_on_field'] is False


def test_field_level_unique_is_reported(schema: Dict[str, Any]) -> None:
    """`@unique` on a field is *not* in `uniqueIndexes`.

    Prisma reports it only as `Field.isUnique`, so a reader that trusts
    `uniqueIndexes` alone silently loses every single-column unique — and with
    it the `accounts_email_key` index the database actually has.
    """
    unique = _unique_named(schema, 'Account', 'email')
    assert unique['db_name'] == 'accounts_email_key'
    assert unique['columns'] == ['email']
    assert unique['is_defined_on_field'] is True

    # ...including when the column is mapped
    mapped = _unique_named(schema, 'Profile', 'accountId')
    assert mapped['db_name'] == 'Profile_account_id_key'
    assert mapped['columns'] == ['account_id']


def _unique_named(schema: Dict[str, Any], model: str, name: str) -> Dict[str, Any]:
    found = [u for u in schema[model]['uniques'] if u['name'] == name]
    assert found, f'{model} has no unique named {name!r}'
    return cast('Dict[str, Any]', found[0])


def test_index_name_is_resolved_when_unnamed(schema: Dict[str, Any]) -> None:
    (index,) = schema['Entry']['indexes']
    assert index['name'] == 'Entry_accountId_idx'
    assert index['is_named'] is False
    assert index['columns'] == ['accountId']


def test_index_name_is_kept_when_mapped(schema: Dict[str, Any]) -> None:
    (index,) = schema['Account']['indexes']
    assert index['name'] == 'account_email_created_idx'
    assert index['is_named'] is True
    assert index['columns'] == ['email', 'createdAt']


def test_id_and_unique_are_not_reported_as_indexes(schema: Dict[str, Any]) -> None:
    """Prisma lists `@id`/`@@id`/`@unique`/`@@unique` in `datamodel.indexes`.

    They are already reported as constraints; emitting them here too would make
    a migration tool create a redundant index alongside every key.
    """
    assert [i['type'] for i in schema['Account']['indexes']] == ['normal']
    assert schema['Composite']['indexes'] == []
    assert schema['Profile']['indexes'] == []


# -- defaults ----------------------------------------------------------------


def test_generator_defaults(schema: Dict[str, Any]) -> None:
    fields = schema['Account']['fields']
    assert fields['id']['default'] == {'kind': 'generator', 'name': 'cuid', 'version': None, 'args': []}
    assert fields['createdAt']['default'] == {'kind': 'generator', 'name': 'now', 'version': None, 'args': []}
    assert schema['Label']['fields']['id']['default'] == {
        'kind': 'generator',
        'name': 'autoincrement',
        'version': None,
        'args': [],
    }


def test_versioned_generator_names_are_split(schema: Dict[str, Any]) -> None:
    """Prisma normalises `@default(uuid())` to `uuid(4)` on the wire.

    The schema text says one thing and the DMMF another. Consumers match on the
    generator, so the version is separated out — leaving it attached made every
    `uuid()` schema fail to build at all, with
    `NotImplementedError: Unhandled Prisma default generator: uuid(4)()`.
    """
    default = schema['Ticket']['fields']['id']['default']
    assert default == {'kind': 'generator', 'name': 'uuid', 'version': '4', 'args': []}


def test_literal_defaults_are_not_confused_with_generators(schema: Dict[str, Any]) -> None:
    fields = schema['Account']['fields']
    # an enum default is the *Python* member name and still needs mapping
    assert fields['role']['default'] == {'kind': 'literal', 'value': 'VIEWER'}
    # BigInt arrives as a string; JSON has no 64-bit integer
    assert fields['visits']['default'] == {'kind': 'literal', 'value': '0'}


def test_updated_at_is_not_a_default(schema: Dict[str, Any]) -> None:
    field = schema['Account']['fields']['updatedAt']
    assert field['is_updated_at'] is True
    assert field['default'] is None


def test_no_default_reports_none(schema: Dict[str, Any]) -> None:
    assert schema['Account']['fields']['email']['default'] is None


# -- relation shapes ---------------------------------------------------------


def test_to_one_owner(schema: Dict[str, Any]) -> None:
    rel = schema['Entry']['relations']['account']
    assert rel['shape'] == 'to-one-owner'
    assert rel['owner'] is True
    assert rel['fk_model'] == 'Entry'
    assert rel['fk_columns'] == ['accountId']
    assert rel['referenced_columns'] == ['id']
    assert rel['back_field'] == 'posts'
    assert rel['on_delete'] == 'Cascade'


def test_to_one_inverse(schema: Dict[str, Any]) -> None:
    """Same arity as `to-one-owner`, opposite physical arrangement.

    `Account.profile` has no column of its own; resolving it means looking at
    the foreign key on `Profile`.
    """
    rel = schema['Account']['relations']['profile']
    assert rel['shape'] == 'to-one-inverse'
    assert rel['owner'] is False
    assert rel['fk_model'] == 'Profile'
    assert rel['fk_columns'] == ['account_id']
    assert rel['referenced_columns'] == ['id']
    assert rel['back_field'] == 'account'


def test_to_many(schema: Dict[str, Any]) -> None:
    rel = schema['Account']['relations']['posts']
    assert rel['shape'] == 'to-many'
    assert rel['owner'] is False
    assert rel['fk_model'] == 'Entry'
    assert rel['fk_columns'] == ['accountId']


def test_self_relation_resolves_both_sides(schema: Dict[str, Any]) -> None:
    """A self-relation puts both sides on the same model.

    Matching by `relationName` alone would pair a field with itself; the pairing
    has to exclude by identity.
    """
    relations = schema['Entry']['relations']
    assert relations['parent']['back_field'] == 'children'
    assert relations['children']['back_field'] == 'parent'
    assert relations['parent']['shape'] == 'to-one-owner'
    assert relations['children']['shape'] == 'to-many'
    assert relations['children']['fk_columns'] == ['parentId']


# -- the P2014 decider -------------------------------------------------------


def test_fk_required_is_derived_for_both_sides(schema: Dict[str, Any]) -> None:
    """Whether `disconnect`/`set` are legal at all.

    `Entry.accountId` is NOT NULL, so no row state expresses "disconnected" and
    Prisma answers P2014. `Entry.parentId` is nullable, so the same operation is
    fine. The list side has to report the same answer as the singular side —
    it is the same column.
    """
    assert schema['Entry']['relations']['account']['fk_required'] is True
    assert schema['Account']['relations']['posts']['fk_required'] is True

    assert schema['Entry']['relations']['parent']['fk_required'] is False
    assert schema['Entry']['relations']['children']['fk_required'] is False


def test_on_delete_absent_means_prisma_default(schema: Dict[str, Any]) -> None:
    """None is "whatever Prisma does for this arity", not "NoAction".

    Prisma defaults to Cascade for a required relation and SetNull for an
    optional one; reading None as NoAction silently changes delete semantics.
    """
    assert schema['Entry']['relations']['parent']['on_delete'] is None
    assert schema['Entry']['relations']['account']['on_delete'] == 'Cascade'


# -- implicit many-to-many ---------------------------------------------------


def test_many_to_many_join_table(schema: Dict[str, Any]) -> None:
    entry_side = schema['Entry']['relations']['labels']
    label_side = schema['Label']['relations']['entries']

    assert entry_side['shape'] == 'many-to-many'
    assert label_side['shape'] == 'many-to-many'

    # `_<relationName>`, and the relation name defaults to the two model names
    # in alphabetical order
    assert entry_side['join_table'] == '_EntryToLabel'
    assert label_side['join_table'] == '_EntryToLabel'

    # column `A` belongs to whichever model sorts first
    assert entry_side['join_self_column'] == 'A'
    assert entry_side['join_other_column'] == 'B'
    assert label_side['join_self_column'] == 'B'
    assert label_side['join_other_column'] == 'A'

    assert entry_side['join_ambiguous'] is False


def test_many_to_many_has_no_foreign_key_columns(schema: Dict[str, Any]) -> None:
    rel = schema['Entry']['relations']['labels']
    assert rel['fk_model'] is None
    assert rel['fk_columns'] == []
    # nothing to null, so `disconnect` is always legal
    assert rel['fk_required'] is False


def test_self_many_to_many_refuses_to_guess(schema: Dict[str, Any]) -> None:
    """Which side is column `A` is not recoverable from the DMMF here.

    Both sides are the same model, so the alphabetical rule does not break the
    tie. Emitting a coin-flip would produce a join that silently returns the
    wrong rows, so the ambiguity is reported instead.
    """
    for name in ('related', 'similar'):
        rel = schema['Label']['relations'][name]
        assert rel['join_table'] == '_similar'
        assert rel['join_ambiguous'] is True
        assert rel['join_self_column'] is None
        assert rel['join_other_column'] is None


# -- enums -------------------------------------------------------------------


def test_enum_value_mapping(datamodel: Datamodel) -> None:
    enums = build_enum_metadata(datamodel)
    assert enums['Role']['db_name'] == 'Role'
    # `@map("administrator")` — writing 'ADMIN' to this column fails
    assert enums['Role']['values'] == {
        'OWNER': 'OWNER',
        'ADMIN': 'administrator',
        'VIEWER': 'VIEWER',
    }


# -- structural ---------------------------------------------------------------


def test_relations_are_not_reported_as_columns(schema: Dict[str, Any]) -> None:
    """Relation fields have no column of their own; the FK scalar does."""
    fields = schema['Entry']['fields']
    assert 'account' not in fields
    assert 'accountId' in fields
    # ...and the FK scalar is flagged read-only, which is what makes writing to
    # it directly an error rather than a silent divergence from the relation
    assert fields['accountId']['is_read_only'] is True


def test_every_model_is_present(schema: Dict[str, Any], datamodel: Datamodel) -> None:
    assert set(schema) == {model.name for model in datamodel.models}


def test_literal_round_trips(schema: Dict[str, Any]) -> None:
    """It is emitted into generated code as source, so it must survive repr()."""
    import ast

    rendered = generator_models.as_literal(schema)
    assert ast.literal_eval(rendered.strip()) == schema
