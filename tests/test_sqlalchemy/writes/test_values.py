"""`prisma.sa.values_for_create` / `values_for_update` without a database.

Every expectation here was first observed from a live Prisma client against
PostgreSQL — see `test_values_live.py`, which re-checks the same claims against
the engine. These are the fast copies, so a regression is visible without a
database, plus the refusals, which have no live counterpart precisely because
the pinned CLI cannot express them.
"""

from __future__ import annotations

import uuid as _uuid
import string
import datetime
from typing import Any, Dict, Iterator

import pytest

from prisma.sa import values_for_create, values_for_update

BASE36 = set(string.digits + string.ascii_lowercase)


@pytest.fixture(name='schema')
def schema_fixture(installed: None) -> Iterator[Dict[str, Any]]:
    """A *copy* of the patched `prisma.metadata.SCHEMA`, for synthetic models.

    A copy because the real one is package-scoped: adding a model to it would
    follow the fixture into every other test in the package.
    """
    import prisma.metadata

    with pytest.MonkeyPatch.context() as patch:
        # `getattr`, not attribute access: the dev client is generated *without*
        # `schemaMetadata`, so `SCHEMA` genuinely does not exist on it and both
        # type checkers are right to say so. The fixtures that populate it run
        # first; the default keeps the failure legible if one does not.
        schema = dict(getattr(prisma.metadata, 'SCHEMA', {}))
        patch.setattr(prisma.metadata, 'SCHEMA', schema, raising=False)
        yield schema


def _field(**overrides: Any) -> Dict[str, Any]:
    field: Dict[str, Any] = {
        'column': 'id',
        'kind': 'scalar',
        'type': 'String',
        'is_list': False,
        'nullable': False,
        'is_id': True,
        'is_unique': False,
        'is_read_only': False,
        'is_updated_at': False,
        'default': None,
        'native_type': None,
        'sequence': None,
    }
    field.update(overrides)
    return field


def _model(**fields: Dict[str, Any]) -> Dict[str, Any]:
    return {
        'table': 'synthetic',
        'primary_key': {'name': None, 'db_name': None, 'fields': [], 'columns': []},
        'fields': fields,
        'relations': {},
        'uniques': [],
        'indexes': [],
    }


def _generator(name: str, version: Any = None, args: Any = None) -> Dict[str, Any]:
    return {'kind': 'generator', 'name': name, 'version': version, 'args': args or []}


# -- client-side generators ---------------------------------------------------


def test_create_fills_cuid_primary_key(installed: None) -> None:
    values = values_for_create('Account', {'email': 'a@b.c', 'slug': 's', 'balance': 1})

    generated = values['id']
    assert isinstance(generated, str)
    # verified against the engine: 25 characters, `c`, lowercase base36
    assert len(generated) == 25
    assert generated.startswith('c')
    assert set(generated) <= BASE36


def test_cuid_timestamp_block_is_the_current_millisecond(installed: None) -> None:
    before = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
    generated = values_for_create('Account', {})['id']
    after = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)

    assert before <= int(generated[1:9], 36) <= after


def test_cuid_counter_block_advances(installed: None) -> None:
    ids = [values_for_create('Account', {})['id'] for _ in range(3)]

    counters = [int(value[9:13], 36) for value in ids]
    assert counters[1] == counters[0] + 1
    assert counters[2] == counters[1] + 1
    # the fingerprint block is per-process, so it must not move
    assert len({value[13:17] for value in ids}) == 1
    assert len(set(ids)) == 3


def test_create_fills_uuid_primary_key(installed: None) -> None:
    values = values_for_create('Ticket', {'reference': 'r'})

    generated = values['id']
    assert isinstance(generated, str), 'a str works for both `uuid` and `text` columns'
    assert _uuid.UUID(generated).version == 4


def test_uuid_version_7_is_time_ordered(schema: Dict[str, Any]) -> None:
    schema['Synthetic'] = _model(id=_field(default=_generator('uuid', version='7')))

    values = [values_for_create('Synthetic', {})['id'] for _ in range(3)]

    assert [_uuid.UUID(value).version for value in values] == [7, 7, 7]
    assert values == sorted(values), 'a v7 uuid sorts by creation time'
    now = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
    assert abs(int(values[0].replace('-', '')[:12], 16) - now) < 5000


def test_nanoid_default_size(schema: Dict[str, Any]) -> None:
    schema['Synthetic'] = _model(id=_field(default=_generator('nanoid')))

    values = [values_for_create('Synthetic', {})['id'] for _ in range(50)]

    assert {len(value) for value in values} == {21}
    alphabet = set('-_' + string.digits + string.ascii_letters)
    assert set(''.join(values)) <= alphabet
    assert len(set(values)) == 50


def test_nanoid_explicit_size(schema: Dict[str, Any]) -> None:
    schema['Synthetic'] = _model(id=_field(default=_generator('nanoid', version='7', args=[7])))

    assert len(values_for_create('Synthetic', {})['id']) == 7


# -- generators that are the database's job -----------------------------------


def test_create_leaves_autoincrement_to_the_database(installed: None) -> None:
    values = values_for_create('Ticket', {'reference': 'r'})

    # both the primary key SERIAL and the explicitly sequenced non-key column
    assert 'ticketNumber' not in values
    assert 'bigNumber' not in values


def test_create_leaves_literal_defaults_to_the_database(installed: None) -> None:
    """`_build` emits a server default for these; filling them would diverge."""
    values = values_for_create('Account', {'email': 'a@b.c'})

    assert 'visits' not in values  # @default(0)
    assert 'role' not in values  # @default(VIEWER)
    # verified against the engine: an undefaulted scalar list is left NULL in
    # the database, and only *presented* as `[]` by the client
    assert 'tags' not in values


def test_create_leaves_undefaulted_fields_alone(installed: None) -> None:
    """A missing required column is the database's error to raise, not a guess."""
    values = values_for_create('Account', {})

    assert 'email' not in values
    assert 'balance' not in values


# -- timestamps ---------------------------------------------------------------


def test_create_fills_now_and_updated_at_with_the_same_instant(installed: None) -> None:
    """Verified against the engine: Prisma sends `now()` client-side too, and
    `createdAt == updatedAt` exactly on a fresh row."""
    values = values_for_create('Account', {'email': 'a@b.c'})

    assert isinstance(values['createdAt'], datetime.datetime)
    assert values['createdAt'] == values['updatedAt']


def test_timestamps_are_naive_utc_for_a_timestamp_column(installed: None) -> None:
    """An aware datetime bound into `timestamp without time zone` is converted
    using the session `TimeZone`, so a non-UTC session would shift it."""
    value = values_for_create('Account', {})['createdAt']

    assert value.tzinfo is None
    delta = abs(value - datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None))
    assert delta < datetime.timedelta(seconds=5)


def test_timestamps_are_aware_for_a_timestamptz_column(schema: Dict[str, Any]) -> None:
    schema['Synthetic'] = _model(
        stamp=_field(
            column='stamp',
            type='DateTime',
            default=_generator('now'),
            native_type=['Timestamptz', ['3']],
        )
    )

    value = values_for_create('Synthetic', {})['stamp']

    assert value.tzinfo is datetime.timezone.utc


def test_timestamps_are_truncated_to_milliseconds(installed: None) -> None:
    """Prisma's engine clock is millisecond resolution and the column is
    `timestamp(3)`; keeping microseconds makes the value we return differ from
    the one the database stores."""
    for _ in range(20):
        assert values_for_create('Account', {})['createdAt'].microsecond % 1000 == 0


def test_one_moment_can_be_shared_across_a_batch(installed: None) -> None:
    """Measured: `create_many` stamps every row in the batch with one instant,
    not one per row."""
    moment = datetime.datetime(2001, 2, 3, 4, 5, 6, 789000, tzinfo=datetime.timezone.utc)

    rows = [values_for_create('Account', {'email': f'{n}@b.c'}, moment=moment) for n in range(3)]

    assert {row['createdAt'] for row in rows} == {moment.replace(tzinfo=None)}
    assert {row['updatedAt'] for row in rows} == {moment.replace(tzinfo=None)}
    assert len({row['id'] for row in rows}) == 3, 'the ids still differ'


def test_a_given_moment_is_read_as_utc_and_truncated(installed: None) -> None:
    naive = datetime.datetime(2001, 2, 3, 4, 5, 6, 789456)

    assert values_for_create('Account', {}, moment=naive)['createdAt'] == naive.replace(microsecond=789000)

    elsewhere = datetime.datetime(2001, 2, 3, 4, 5, 6, tzinfo=datetime.timezone(datetime.timedelta(hours=5)))
    assert values_for_update('Account', {}, moment=elsewhere)['updatedAt'] == datetime.datetime(2001, 2, 2, 23, 5, 6)


def test_update_refreshes_updated_at(installed: None) -> None:
    values = values_for_update('Account', {'email': 'a@b.c'})

    assert 'updatedAt' in values
    assert values['email'] == 'a@b.c'


def test_update_does_not_fill_create_side_defaults(installed: None) -> None:
    values = values_for_update('Account', {'email': 'a@b.c'})

    assert 'id' not in values
    assert 'createdAt' not in values


# -- explicit values win ------------------------------------------------------


def test_explicit_values_win_on_create(installed: None) -> None:
    """Verified against the engine: an explicit `updatedAt` is not overwritten."""
    moment = datetime.datetime(2001, 2, 3, 4, 5, 6)
    values = values_for_create('Account', {'id': 'given', 'updatedAt': moment, 'createdAt': moment})

    assert values['id'] == 'given'
    assert values['updatedAt'] == moment
    assert values['createdAt'] == moment


def test_explicit_updated_at_wins_on_update(installed: None) -> None:
    moment = datetime.datetime(2001, 2, 3, 4, 5, 6)

    assert values_for_update('Account', {'updatedAt': moment})['updatedAt'] == moment


def test_explicit_none_is_not_a_missing_value(schema: Dict[str, Any]) -> None:
    schema['Synthetic'] = _model(
        note=_field(column='note', nullable=True, is_id=False, default=_generator('cuid')),
    )

    assert values_for_create('Synthetic', {'note': None})['note'] is None


# -- names and enums ----------------------------------------------------------


def test_field_names_become_column_names(installed: None) -> None:
    values = values_for_create('Account', {'slug': 'abc'})

    assert values['url_slug'] == 'abc'
    assert 'slug' not in values


def test_enum_values_become_stored_labels(installed: None) -> None:
    # what a generated client hands you: `class Role(StrEnum): ADMIN = 'ADMIN'`
    from prisma._compat import StrEnum

    class Role(StrEnum):
        ADMIN = 'ADMIN'

    assert values_for_create('Account', {'role': 'ADMIN'})['role'] == 'administrator'
    assert values_for_update('Account', {'role': Role.ADMIN})['role'] == 'administrator'
    assert values_for_create('Account', {'role': 'VIEWER'})['role'] == 'VIEWER'


def test_unmapped_enum_member_is_refused(installed: None) -> None:
    with pytest.raises(LookupError, match='administrator'):
        # the *stored label*, not the member name — Prisma takes the member name
        values_for_create('Account', {'role': 'administrator'})


def test_enum_list_values_become_labels(schema: Dict[str, Any]) -> None:
    schema['Synthetic'] = _model(
        roles=_field(column='roles', kind='enum', type='Role', is_list=True, is_id=False),
    )

    assert values_for_create('Synthetic', {'roles': ['ADMIN', 'OWNER']})['roles'] == ['administrator', 'OWNER']


# -- refusals -----------------------------------------------------------------


def test_unknown_field_is_refused(installed: None) -> None:
    with pytest.raises(LookupError, match='nope'):
        values_for_create('Account', {'nope': 1})

    with pytest.raises(LookupError, match='nope'):
        values_for_update('Account', {'nope': 1})


def test_relation_field_is_refused(installed: None) -> None:
    with pytest.raises(LookupError, match='posts'):
        values_for_create('Account', {'posts': {'create': []}})


def test_unknown_model_is_refused(installed: None) -> None:
    with pytest.raises(LookupError, match='Nope'):
        values_for_create('Nope', {})


def test_ulid_is_refused(schema: Dict[str, Any]) -> None:
    """The pinned CLI does not accept `@default(ulid())`, so its output could
    not be observed and must not be invented."""
    schema['Synthetic'] = _model(id=_field(default=_generator('ulid')))

    with pytest.raises(NotImplementedError, match='ulid'):
        values_for_create('Synthetic', {})


def test_mongo_auto_is_refused(schema: Dict[str, Any]) -> None:
    schema['Synthetic'] = _model(id=_field(default=_generator('auto')))

    with pytest.raises(NotImplementedError, match='auto'):
        values_for_create('Synthetic', {})


def test_unknown_generator_is_refused(schema: Dict[str, Any]) -> None:
    schema['Synthetic'] = _model(id=_field(default=_generator('sequence')))

    with pytest.raises(NotImplementedError, match='sequence'):
        values_for_create('Synthetic', {})


def test_versioned_cuid_is_refused(schema: Dict[str, Any]) -> None:
    """`cuid(2)` is a different alphabet and length; the pinned CLI rejects it,
    so there is nothing to copy."""
    schema['Synthetic'] = _model(id=_field(default=_generator('cuid', version='2')))

    with pytest.raises(NotImplementedError, match='cuid'):
        values_for_create('Synthetic', {})


def test_unknown_uuid_version_is_refused(schema: Dict[str, Any]) -> None:
    schema['Synthetic'] = _model(id=_field(default=_generator('uuid', version='8')))

    with pytest.raises(NotImplementedError, match='uuid'):
        values_for_create('Synthetic', {})


def test_timestamp_on_an_unverified_native_type_is_refused(schema: Dict[str, Any]) -> None:
    schema['Synthetic'] = _model(
        day=_field(column='day', type='DateTime', default=_generator('now'), native_type=['Date', []]),
    )

    with pytest.raises(NotImplementedError, match='day'):
        values_for_create('Synthetic', {})


def test_atomic_update_operations_are_refused(installed: None) -> None:
    """`{'increment': 1}` is on the runbook's STOP list. Passing the dict
    through would write the dict."""
    with pytest.raises(NotImplementedError, match='visits'):
        values_for_update('Account', {'visits': {'increment': 1}})


def test_json_fields_still_accept_dicts(installed: None) -> None:
    assert values_for_create('Account', {'payload': {'a': 1}})['payload'] == {'a': 1}
