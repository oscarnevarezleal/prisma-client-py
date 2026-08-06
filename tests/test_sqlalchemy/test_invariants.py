"""Properties that must hold for *any* schema, not just the reference one.

The rest of the suite asserts specific values against a 6-model fixture. These
assert relationships that have to hold whatever the schema is — every foreign
key column exists, every referenced column exists, the two sides of a relation
agree — so they keep working when this is pointed at a real production schema
with a hundred models and shapes the fixture never had.

They are the cheapest thing in the suite to run against a new schema: swap the
`generated` fixture's source and everything here still means something.
"""

from __future__ import annotations

from typing import Any, Dict, cast

import sqlalchemy as sa

# -- the metadata dict is internally consistent -------------------------------


def test_relation_foreign_keys_name_real_fields(generated: Dict[str, Any]) -> None:
    """`fk_fields` must resolve on `fk_model`, not on the model declaring it.

    Getting this backwards is the easiest mistake in the derivation: the
    inverse side of a relation reports the *other* model's columns.
    """
    schema = generated['schema']
    for model, spec in schema.items():
        for name, relation in spec['relations'].items():
            if relation['fk_model'] is None:
                continue

            owner = schema[relation['fk_model']]
            for field in relation['fk_fields']:
                assert field in owner['fields'], f'{model}.{name}: {relation["fk_model"]}.{field} does not exist'


def _referenced_model(model: str, relation: Dict[str, Any]) -> str:
    """Which model `referenced_fields` belong to.

    The owner side references the model it points at. The inverse side is
    described by the *other* model's foreign key, which points back here — so
    its `referenced_fields` are this model's own fields. Getting this backwards
    is invisible in a schema where both models happen to have an `id`.
    """
    return cast(str, relation['to']) if relation['owner'] else model


def test_referenced_columns_exist_on_the_target(generated: Dict[str, Any]) -> None:
    schema = generated['schema']
    for model, spec in schema.items():
        for name, relation in spec['relations'].items():
            if relation['fk_model'] is None:
                continue

            target = schema[_referenced_model(model, relation)]
            columns = {f['column'] for f in target['fields'].values()}
            for column in relation['referenced_columns']:
                assert column in columns, f'{model}.{name} references {target["table"]}.{column}, which does not exist'

            fields = set(target['fields'])
            for field in relation['referenced_fields']:
                assert field in fields, f'{model}.{name} references unknown field {field}'


def test_fk_columns_and_fields_line_up(generated: Dict[str, Any]) -> None:
    """One column per field, in the same order — they are used positionally."""
    for spec in generated['schema'].values():
        for relation in spec['relations'].values():
            assert len(relation['fk_fields']) == len(relation['fk_columns'])
            assert len(relation['referenced_fields']) == len(relation['referenced_columns'])


def test_both_sides_of_a_relation_agree(generated: Dict[str, Any]) -> None:
    """The two sides describe one physical arrangement.

    They must report the same foreign key and the same nullability, because it
    is the same column. `fk_required` disagreeing across sides is what lets a
    planner allow `disconnect` on the list side and orphan rows.
    """
    schema = generated['schema']
    for model, spec in schema.items():
        for name, relation in spec['relations'].items():
            # Prisma will not validate a schema whose relation is declared on
            # only one side, so a missing back reference means the derivation
            # lost it — which is the bug this test exists to catch, not a case
            # to skip past.
            back = relation['back_field']
            assert back is not None, f'{model}.{name} has no back reference'

            other = schema[relation['to']]['relations'][back]
            assert other['to'] == model, f'{model}.{name} <-> {relation["to"]}.{back} do not point at each other'
            assert other['relation_name'] == relation['relation_name']
            assert other['fk_model'] == relation['fk_model']
            assert other['fk_columns'] == relation['fk_columns']
            assert (
                other['fk_required'] == relation['fk_required']
            ), f'{model}.{name} and {relation["to"]}.{back} disagree on fk_required'
            # exactly one side owns a foreign key, or neither does (m2m)
            assert not (relation['owner'] and other['owner'])


def test_exactly_one_owner_per_non_m2m_relation(generated: Dict[str, Any]) -> None:
    schema = generated['schema']
    for model, spec in schema.items():
        for name, relation in spec['relations'].items():
            if relation['shape'] == 'many-to-many':
                assert relation['fk_model'] is None
                continue

            back = relation['back_field']
            assert back is not None, f'{model}.{name} has no back-relation field'
            other = schema[relation['to']]['relations'][back]
            assert relation['owner'] ^ other['owner'], f'{model}.{name}: relation has no single owner'


def test_index_and_unique_columns_exist(generated: Dict[str, Any]) -> None:
    for model, spec in generated['schema'].items():
        columns = {field['column'] for field in spec['fields'].values()}
        for index in spec['indexes']:
            assert set(index['columns']) <= columns, f'{model}: index {index["name"]} names unknown columns'
        for unique in spec['uniques']:
            assert set(unique['columns']) <= columns, f'{model}: unique {unique["db_name"]} names unknown columns'


def test_primary_key_columns_exist(generated: Dict[str, Any]) -> None:
    for model, spec in generated['schema'].items():
        columns = {field['column'] for field in spec['fields'].values()}
        assert set(spec['primary_key']['columns']) <= columns, f'{model}: primary key names unknown columns'


def test_enum_fields_reference_declared_enums(generated: Dict[str, Any]) -> None:
    enums = generated['enums']
    for model, spec in generated['schema'].items():
        for name, field in spec['fields'].items():
            if field['kind'] == 'enum':
                assert field['type'] in enums, f'{model}.{name} uses undeclared enum {field["type"]}'


def test_constraint_names_are_unique_per_table(generated: Dict[str, Any]) -> None:
    """PostgreSQL shares one namespace between indexes and constraints.

    A collision is a `CREATE` that fails on a database the user already has.
    """
    for model, spec in generated['schema'].items():
        names = [index['name'] for index in spec['indexes']] + [unique['db_name'] for unique in spec['uniques']]
        assert len(names) == len(set(names)), f'{model}: duplicate constraint names {names}'


# -- the built MetaData matches the metadata dict ------------------------------


def test_every_model_became_a_table(generated: Dict[str, Any], metadata: sa.MetaData) -> None:
    for spec in generated['schema'].values():
        assert spec['table'] in metadata.tables


def test_every_scalar_field_became_a_column(generated: Dict[str, Any], metadata: sa.MetaData) -> None:
    for spec in generated['schema'].values():
        table = metadata.tables[spec['table']]
        expected = [field['column'] for field in spec['fields'].values()]
        # order matters: pg_dump compares column order, and Alembic does not
        assert list(table.c.keys()) == expected, f'{spec["table"]}: column set or order differs'


def test_no_extra_tables(generated: Dict[str, Any], metadata: sa.MetaData) -> None:
    """Only models and implicit m2m join tables — nothing invented."""
    models = {spec['table'] for spec in generated['schema'].values()}
    joins = {
        relation['join_table']
        for spec in generated['schema'].values()
        for relation in spec['relations'].values()
        if relation['shape'] == 'many-to-many'
    }
    assert set(metadata.tables) == models | joins


def test_every_foreign_key_resolves(metadata: sa.MetaData) -> None:
    """A `ForeignKey` naming a missing table raises only at use time."""
    for table in metadata.tables.values():
        for fk in table.foreign_keys:
            # raises NoReferencedTableError / NoReferencedColumnError if broken
            assert fk.column is not None


def test_every_index_belongs_to_its_table(metadata: sa.MetaData) -> None:
    for table in metadata.tables.values():
        for index in table.indexes:
            assert index.name, f'{table.name} has an unnamed index'
            for column in index.columns:
                assert column.table is table


def test_nullable_matches_the_metadata(generated: Dict[str, Any], metadata: sa.MetaData) -> None:
    for spec in generated['schema'].values():
        table = metadata.tables[spec['table']]
        for field in spec['fields'].values():
            expected = field['nullable'] or field['is_list']
            assert table.c[field['column']].nullable is expected, f'{spec["table"]}.{field["column"]}'
