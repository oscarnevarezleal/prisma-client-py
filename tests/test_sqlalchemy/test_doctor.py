"""What the doctor claims, and what it refuses to claim.

Every `verified` here has to trace to a row in §4.1 of
`docs/prisma-to-sqlalchemy-runbook.md`, and every `stop` to a row in §5. The
tests that matter most are the ones where the *method name* is on the verified
list and the *arguments* are not: `update(data={'n': {'increment': 1}})` is an
atomic operation, and calling it migratable would be a lie a team would only
discover in production.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List
from pathlib import Path
from textwrap import dedent

import pytest

from prisma.sa import _doctor
from tests.utils import Runner
from prisma.sa._scan import ACTIONS, scan_source
from prisma.sa._doctor import Finding, Diagnosis, to_json, diagnose, render_text, schema_facts

PREAMBLE = """
from prisma import Prisma

db = Prisma()

async def main():
"""


def classify(call: str) -> Finding:
    """Classify a single call written as it would appear in a function body."""
    source = PREAMBLE + f'    await {call}\n'
    result = scan_source(dedent(source), 'app.py')
    assert not result.unparsed
    described = [site.action for site in result.call_sites]
    assert len(result.call_sites) == 1, described
    return _doctor.classify(result.call_sites[0])


def statuses(call: str) -> str:
    return classify(call).status


def diagnose_source(source: str, path: str = 'app.py') -> Diagnosis:
    return diagnose(scan_source(dedent(source), path), schema=_doctor.SchemaFacts.unavailable('not generated'))


# --------------------------------------------------------------------------
# verified — each traceable to a §4.1 row
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    'call',
    [
        "db.user.find_unique(where={'id': 1})",
        "db.user.find_unique(where={'siteId_slug': {'siteId': 1, 'slug': 'a'}})",
        "db.user.find_first(order={'createdAt': 'desc'})",
        "db.user.find_many(take=5, skip=10, order={'id': 'asc'})",
        "db.user.find_many(distinct=['status'])",
        "db.user.find_many(where={'name': {'contains': 'a', 'mode': 'insensitive'}})",
        "db.user.find_many(where={'AND': [{'a': 1}, {'b': {'gte': 2}}]})",
        "db.user.find_many(where={'deletedAt': None})",
        "db.user.find_many(where={'comments': {'some': {'ok': True}}})",
        "db.user.create(data={'name': 'a'})",
        "db.user.update(where={'id': 1}, data={'name': 'a'})",
        "db.user.delete(where={'id': 1})",
        "db.user.create_many(data=[{'name': 'a'}, {'name': 'b'}])",
        "db.user.create_many(data=[{'name': 'a'}], skip_duplicates=True)",
        "db.user.update_many(where={'ok': True}, data={'name': 'a'})",
        'db.user.delete_many()',
        'db.user.count()',
        "db.user.count(where={'isHidden': False})",
        "db.user.group_by(['status'], count={'id': True})",
        'db.tx()',
        'db.batch_()',
    ],
)
def test_verified_call_sites(call: str) -> None:
    finding = classify(call)
    assert finding.status == 'verified', finding.reason
    assert finding.reference.startswith('§4.1')


def test_a_good_upsert_is_verified_and_carries_its_precondition() -> None:
    finding = classify(
        "db.user.upsert(where={'id': 1}, data={'create': {'id': 1, 'name': 'a'}, 'update': {'name': 'b'}})"
    )
    assert finding.status == 'verified'
    assert [note for note in finding.notes if 'precondition' in note]


def test_max_wait_is_verified_with_the_pool_timeout_note() -> None:
    finding = classify('db.tx(max_wait=1000)')
    assert finding.status == 'verified'
    assert [note for note in finding.notes if 'pool_timeout' in note]


# --------------------------------------------------------------------------
# stop — the arguments, not the method name
# --------------------------------------------------------------------------


def test_an_atomic_update_is_a_stop_even_though_update_is_verified() -> None:
    """The whole reason the doctor parses arguments."""
    assert statuses("db.user.update(where={'id': 1}, data={'name': 'a'})") == 'verified'

    finding = classify("db.user.update(where={'id': 1}, data={'views': {'increment': 1}})")
    assert finding.status == 'stop'
    assert finding.code == 'atomic-update'
    assert 'increment' in finding.reason
    assert finding.reference.startswith('§5')


def test_an_atomic_update_many_is_a_stop() -> None:
    finding = classify("db.user.update_many(where={'ok': True}, data={'views': {'decrement': 1}})")
    assert finding.code == 'atomic-update'


@pytest.mark.parametrize(
    ('call', 'code'),
    [
        ("db.user.create(data={'name': 'a', 'posts': {'create': [{'title': 't'}]}})", 'nested-write'),
        ("db.user.create(data={'name': 'a', 'org': {'connect': {'id': 1}}})", 'relation-connect'),
        ("db.user.create(data={'org': {'connectOrCreate': {'where': {}, 'create': {}}}})", 'relation-connect'),
        ("db.user.update(where={'id': 1}, data={'org': {'disconnect': True}})", 'relation-connect'),
        ("db.user.create_many(data=[{'name': 'a'}, {'posts': {'create': []}}])", 'nested-write'),
        ("db.user.find_many(where={'meta': {'path': ['a'], 'equals': 1}})", 'json-filter'),
        ("db.user.find_many(where={'tags': {'has': 'x'}})", 'scalar-list-filter'),
        ("db.user.find_many(where={'body': {'search': 'cat'}})", 'full-text-search'),
        ('db.user.aggregate()', 'aggregate'),
        ("db.user.group_by(['status'], sum={'views': True})", 'aggregate'),
        ('db.tx(timeout=5000)', 'transaction-timeout'),
    ],
)
def test_stop_call_sites(call: str, code: str) -> None:
    finding = classify(call)
    assert finding.status == 'stop', finding.reason
    assert finding.code == code
    assert finding.reference.startswith('§5')


@pytest.mark.parametrize(
    ('call', 'code'),
    [
        (
            "db.user.upsert(where={'id': 1}, data={'create': {'id': 1}, 'update': {}})",
            'upsert-empty-update',
        ),
        (
            "db.user.upsert(where={'slug': 'left'}, data={'create': {'slug': 'right'}, 'update': {'a': 1}})",
            'upsert-where-mismatch',
        ),
        (
            "db.user.upsert(where={'slug': 'left'}, data={'create': {}, 'update': {'a': 1}}, include={'x': True})",
            'upsert-include',
        ),
        (
            "db.user.upsert(where={'a_b': {'a': 1, 'b': 2}}, data={'create': {'a': 1}, 'update': {'x': 1}})",
            'upsert-where-mismatch',
        ),
    ],
)
def test_upsert_stops(call: str, code: str) -> None:
    finding = classify(call)
    assert finding.status == 'stop', finding.reason
    assert finding.code == code


def test_a_compound_upsert_that_satisfies_the_precondition_is_verified() -> None:
    finding = classify(
        "db.user.upsert(where={'a_b': {'a': 1, 'b': 2}}, data={'create': {'a': 1, 'b': 2}, 'update': {'x': 1}})"
    )
    assert finding.status == 'verified'


# --------------------------------------------------------------------------
# unknown — neither list covers it, and saying so is the point
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ('call', 'code'),
    [
        ('db.user.find_many(where=filters)', 'opaque-where'),
        ('db.user.create(data=payload)', 'opaque-data'),
        ("db.user.update(where={'id': 1}, data=payload)", 'opaque-data'),
        ('db.user.create_many(data=rows)', 'opaque-data'),
        ('db.user.find_many(**kwargs)', 'opaque-arguments'),
        ("db.user.find_many(cursor={'id': 1})", 'argument-not-covered'),
        ("db.user.create(data={'a': 1}, include={'posts': True})", 'argument-not-covered'),
        ('db.user.count(take=5)', 'argument-not-covered'),
        ("db.user.find_many(where={'a': {'equals': 1}})", 'where-operator-not-covered'),
        ("db.user.find_many(where={'a': {'not_in': [1]}})", 'where-operator-not-covered'),
        ("db.user.find_unique_or_raise(where={'id': 1})", 'action-not-covered'),
        ("db.query_raw('SELECT 1')", 'raw-sql'),
        ("db.execute_raw('DELETE FROM x')", 'raw-sql'),
    ],
)
def test_unknown_call_sites(call: str, code: str) -> None:
    finding = classify(call)
    assert finding.status == 'unknown', finding.reason
    assert finding.code == code


def test_a_dynamic_model_is_unknown_and_counted() -> None:
    source = PREAMBLE + '    await getattr(db, name).find_many()\n'
    result = scan_source(dedent(source), 'app.py')
    finding = _doctor.classify(result.call_sites[0])
    assert finding.status == 'unknown'
    assert finding.code == 'dynamic-model'


def test_an_unresolved_client_is_unknown() -> None:
    result = scan_source('async def main(store):\n    await store.user.find_many()\n', 'app.py')
    finding = _doctor.classify(result.call_sites[0])
    assert finding.status == 'unknown'
    assert finding.code == 'unresolved-client'


def test_a_visible_stop_beats_an_opaque_argument() -> None:
    """Knowing it is blocked is more informative than knowing it is unclear."""
    finding = classify("db.user.update(where=filters, data={'n': {'increment': 1}})")
    assert finding.status == 'stop'
    assert finding.code == 'atomic-update'


# --------------------------------------------------------------------------
# transactions migrate whole, or not at all
# --------------------------------------------------------------------------


def test_a_stop_inside_a_transaction_blocks_the_whole_block() -> None:
    diagnosis = diagnose_source("""
        from prisma import Prisma

        db = Prisma()

        async def main():
            async with db.tx() as tx:
                await tx.user.create(data={'name': 'a'})
                await tx.user.update(where={'id': 1}, data={'views': {'increment': 1}})
            await db.user.create(data={'name': 'outside'})
    """)
    by_line = {finding.site.line: finding for finding in diagnosis.findings}
    assert by_line[7].status == 'stop'  # the `tx()` itself
    assert by_line[8].status == 'stop'  # a create that is verified on its own
    assert by_line[8].code == 'transaction-blocked'
    assert by_line[9].status == 'stop'  # the atomic update that caused it
    assert by_line[10].status == 'verified'  # outside the block

    assert [block.blocked for block in diagnosis.transactions] == [True]


def test_a_clean_transaction_stays_verified() -> None:
    diagnosis = diagnose_source("""
        from prisma import Prisma

        db = Prisma()

        async def main():
            async with db.tx() as tx:
                await tx.user.create(data={'name': 'a'})
    """)
    assert {finding.status for finding in diagnosis.findings} == {'verified'}
    assert diagnosis.transactions[0].blocked is False


def test_a_transaction_without_a_visible_body_is_a_caveat() -> None:
    diagnosis = diagnose_source("""
        from prisma import Prisma

        db = Prisma()

        async def main():
            tx = await db.tx().start()
            await tx.user.create(data={'name': 'a'})
    """)
    codes = [caveat.code for caveat in diagnosis.caveats]
    assert 'transaction-body-not-visible' in codes


# --------------------------------------------------------------------------
# phases
# --------------------------------------------------------------------------


def test_modules_are_sized_into_phases(tmp_path: Path) -> None:
    write(tmp_path, 'clean.py', "db.user.find_many()\ndb.user.create(data={'a': 1})\n")
    write(
        tmp_path,
        'one_blocker.py',
        "db.user.find_many()\ndb.user.update(where={'i': 1}, data={'n': {'increment': 1}})\n",
    )
    write(tmp_path, 'blocked.py', 'db.user.aggregate()\ndb.tx(timeout=1)\n')
    write(tmp_path, 'murky.py', 'db.user.find_many(where=w)\ndb.user.find_many()\n')

    diagnosis = _doctor.diagnose_paths([tmp_path], root=tmp_path, schema_paths=[])
    phases = {group.key: group.phase for group in diagnosis.modules}
    assert phases == {
        'clean.py': 'migratable-now',
        'one_blocker.py': 'one-blocker',
        'blocked.py': 'blocked',
        'murky.py': 'mixed',
    }


def test_models_are_grouped_too(tmp_path: Path) -> None:
    write(tmp_path, 'a.py', 'db.user.find_many()\ndb.post.aggregate()\n')
    diagnosis = _doctor.diagnose_paths([tmp_path], root=tmp_path, schema_paths=[])
    phases = {group.key: group.phase for group in diagnosis.models}
    assert phases == {'user': 'migratable-now', 'post': 'blocked'}


def write(root: Path, name: str, body: str) -> None:
    (root / name).write_text('from prisma import Prisma\n\ndb = Prisma()\n\n' + body)


# --------------------------------------------------------------------------
# the summary says what it could not see
# --------------------------------------------------------------------------


def test_caveats_count_everything_the_scan_could_not_analyse(tmp_path: Path) -> None:
    write(tmp_path, 'a.py', 'db.user.find_many(where=w)\ndb.user.find_many(**kwargs)\n')
    write(tmp_path, 'b.py', 'x = getattr(db, name).find_many()\n')
    (tmp_path / 'broken.py').write_text('def (:\n')
    (tmp_path / 'c.py').write_text('async def go(store):\n    await store.user.find_many()\n')

    diagnosis = _doctor.diagnose_paths([tmp_path], root=tmp_path, schema_paths=[])
    counts = {caveat.code: caveat.count for caveat in diagnosis.caveats}
    assert counts['unparsed-file'] == 1
    assert counts['opaque-arguments'] >= 1
    assert counts['dynamic-model'] == 1
    assert counts['unresolved-client'] == 1

    text = render_text(diagnosis)
    assert 'could not analyse' in text.lower()
    assert 'broken.py' in text


def test_the_text_report_names_the_reason_for_every_stop(tmp_path: Path) -> None:
    write(tmp_path, 'a.py', "db.user.update(where={'i': 1}, data={'n': {'increment': 1}})\n")
    diagnosis = _doctor.diagnose_paths([tmp_path], root=tmp_path, schema_paths=[])
    text = render_text(diagnosis)
    assert 'a.py:5' in text
    assert 'Atomic updates' in text
    assert '§5' in text


# --------------------------------------------------------------------------
# schema facts
# --------------------------------------------------------------------------


@pytest.mark.usefixtures('installed')
def test_schema_facts_from_build_metadata() -> None:
    facts = schema_facts([])
    assert facts.available is True
    assert facts.provider == 'postgresql'
    assert facts.models > 0
    assert facts.tables >= facts.models
    assert sum(facts.relation_shapes.values()) > 0
    assert 'to-one-owner' in facts.relation_shapes


@pytest.mark.usefixtures('installed')
def test_schema_facts_report_paths_with_no_verified_translation() -> None:
    """A self-referential implicit m2m is §5: which side is join column `A` is a
    coin flip, so the reference schema's one is a blocker the doctor must name."""
    facts = schema_facts([])
    codes = [blocker.code for blocker in facts.blockers]
    assert 'self-referential-m2m' in codes


def test_schema_facts_without_a_generated_client() -> None:
    facts = schema_facts([], available=False)
    assert facts.available is False
    assert 'schemaMetadata' in facts.detail


def test_scope_check_reads_the_raw_schema(tmp_path: Path) -> None:
    schema = tmp_path / 'schema.prisma'
    schema.write_text(
        dedent("""
        datasource db {
          provider = "postgresql"
          url      = env("DATABASE_URL")
        }

        model User {
          id   String @id @db.Uuid
          name String @db.VarChar(255)
          // a comment mentioning @db.Ignored
        }
        """)
    )
    facts = schema_facts([schema], available=False)
    assert facts.scope.provider == 'postgresql'
    assert facts.scope.relation_mode is None
    assert facts.scope.native_types == {'Uuid': 1, 'VarChar': 1}
    assert [check.name for check in facts.scope.checks if check.status == 'pass'] == [
        'provider is PostgreSQL',
        'relationMode is not "prisma"',
    ]


def test_scope_check_fails_a_non_postgres_provider(tmp_path: Path) -> None:
    schema = tmp_path / 'schema.prisma'
    schema.write_text('datasource db {\n  provider = "mysql"\n  relationMode = "prisma"\n}\n')
    facts = schema_facts([schema], available=False)
    failed = [check.name for check in facts.scope.checks if check.status == 'fail']
    assert failed == ['provider is PostgreSQL', 'relationMode is not "prisma"']


def test_scope_check_walks_a_schema_folder(tmp_path: Path) -> None:
    folder = tmp_path / 'prisma'
    folder.mkdir()
    (folder / 'datasource.prisma').write_text('datasource db {\n  provider = "postgresql"\n}\n')
    (folder / 'user.prisma').write_text('model User {\n  id String @id @db.Uuid\n}\n')
    facts = schema_facts([folder], available=False, root=tmp_path)
    assert facts.scope.provider == 'postgresql'
    assert sorted(facts.scope.paths) == ['prisma/datasource.prisma', 'prisma/user.prisma']


def test_scope_check_with_no_schema_at_all() -> None:
    facts = schema_facts([], available=False)
    assert facts.scope.paths == []
    assert [check.status for check in facts.scope.checks] == ['unknown', 'unknown']


# --------------------------------------------------------------------------
# the JSON interface
# --------------------------------------------------------------------------


def test_json_shape(tmp_path: Path) -> None:
    write(
        tmp_path,
        'a.py',
        "db.user.find_many()\ndb.user.update(where={'i': 1}, data={'n': {'increment': 1}})\ndb.user.find_many(where=w)\n",
    )
    diagnosis = _doctor.diagnose_paths([tmp_path], root=tmp_path, schema_paths=[])
    payload: Dict[str, Any] = json.loads(json.dumps(to_json(diagnosis)))

    assert payload['doctor']['schema_version'] == 1
    assert payload['summary']['call_sites'] == 3
    assert payload['summary']['verified'] == 1
    assert payload['summary']['stop'] == 1
    assert payload['summary']['unknown'] == 1
    assert payload['summary']['files_scanned'] == 1

    sites: List[Dict[str, Any]] = payload['call_sites']
    assert [site['status'] for site in sites] == ['verified', 'stop', 'unknown']
    assert sites[1]['reference'].startswith('§5')
    assert sites[1]['code'] == 'atomic-update'
    assert sites[0]['path'] == 'a.py'
    assert sites[2]['opaque_arguments'] == []
    assert sites[2]['arguments'] == ['where']

    assert payload['modules'][0]['phase'] == 'mixed'
    assert payload['schema']['available'] is False
    assert isinstance(payload['caveats'], list)


def test_json_and_text_agree_on_the_counts(tmp_path: Path) -> None:
    write(tmp_path, 'a.py', 'db.user.find_many()\ndb.user.aggregate()\n')
    diagnosis = _doctor.diagnose_paths([tmp_path], root=tmp_path, schema_paths=[])
    payload = to_json(diagnosis)
    text = render_text(diagnosis)
    assert '2 call sites' in text
    assert payload['summary']['call_sites'] == 2
    assert '1 verified' in text
    assert '1 stop' in text


def test_call_site_json_is_a_stable_key_set(tmp_path: Path) -> None:
    """A Claude skill consumes this; adding a key is fine, renaming one is not."""
    write(tmp_path, 'a.py', 'db.user.find_many()\n')
    diagnosis = _doctor.diagnose_paths([tmp_path], root=tmp_path, schema_paths=[])
    site = to_json(diagnosis)['call_sites'][0]
    assert set(site) == {
        'path',
        'line',
        'column',
        'entry',
        'action',
        'receiver',
        'model',
        'resolution',
        'block',
        'arguments',
        'opaque_arguments',
        'status',
        'code',
        'reason',
        'reference',
        'notes',
    }


# --------------------------------------------------------------------------
# the command
# --------------------------------------------------------------------------


def test_doctor_command(runner: Runner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write(tmp_path, 'a.py', 'db.user.find_many()\ndb.user.aggregate()\n')
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(['py', 'sqlalchemy', 'doctor'])
    assert result.exit_code == 0, result.output
    assert '2 call sites' in result.output
    assert '§5 aggregate()' in result.output
    assert 'could not analyse' in result.output.lower()


def test_doctor_command_json(runner: Runner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write(tmp_path, 'a.py', 'db.user.find_many()\n')
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(['py', 'sqlalchemy', 'doctor', '--json'])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload['summary']['verified'] == 1
    assert payload['doctor']['runbook'] == 'docs/prisma-to-sqlalchemy-runbook.md'


def test_doctor_command_honours_path_and_schema(
    runner: Runner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / 'src'
    source.mkdir()
    write(source, 'a.py', 'db.user.find_many()\n')
    write(tmp_path, 'ignored.py', 'db.user.aggregate()\n')
    (tmp_path / 'schema.prisma').write_text('datasource db {\n  provider = "postgresql"\n}\n')
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(['py', 'sqlalchemy', 'doctor', '--json', '--path', 'src'])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload['summary']['call_sites'] == 1
    assert payload['schema']['scope_check']['paths'] == ['schema.prisma']
    assert payload['schema']['scope_check']['provider'] == 'postgresql'


def test_doctor_command_discovers_a_schema_folder(
    runner: Runner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / 'prisma'
    folder.mkdir()
    (folder / 'db.prisma').write_text('datasource db {\n  provider = "postgresql"\n  relationMode = "prisma"\n}\n')
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(['py', 'sqlalchemy', 'doctor', '--json'])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload['schema']['scope_check']['paths'] == ['prisma/db.prisma']
    failed = [check for check in payload['schema']['scope_check']['checks'] if check['status'] == 'fail']
    assert [check['name'] for check in failed] == ['relationMode is not "prisma"']


def test_doctor_command_with_no_schema_anywhere(
    runner: Runner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(tmp_path, 'a.py', 'db.user.find_many()\n')
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(['py', 'sqlalchemy', 'doctor', '--json'])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload['schema']['scope_check']['paths'] == []
    assert {check['status'] for check in payload['schema']['scope_check']['checks']} == {'unknown'}


def test_the_text_report_truncates_long_listings_but_says_by_how_much(tmp_path: Path) -> None:
    """`--json` is the complete record; the text report is for reading."""
    calls = 'db.user.find_many()\n' * (_doctor.MAX_LISTED + 3)
    write(tmp_path, 'a.py', calls)

    diagnosis = _doctor.diagnose_paths([tmp_path], root=tmp_path, schema_paths=[])
    text = render_text(diagnosis)
    assert f'({_doctor.MAX_LISTED + 3})' in text
    assert '... and 3 more — `--json` lists every one' in text
    assert len(to_json(diagnosis)['call_sites']) == _doctor.MAX_LISTED + 3


def test_include_on_a_read_is_verified_but_flagged_as_a_reshape() -> None:
    """§4.1 covers it, and says a to-many `include` is two queries, not a join."""
    finding = classify("db.user.find_many(include={'posts': True})")
    assert finding.status == 'verified'
    assert [note for note in finding.notes if 'to-many' in note]


def test_the_json_summary_states_what_was_not_analysed(tmp_path: Path) -> None:
    """A summary that only counts what it understood overstates the scan."""
    write(tmp_path, 'a.py', 'db.user.find_many()\ndb.user.find_many(where=w)\n')
    (tmp_path / 'broken.py').write_text('def (:\n')

    summary = _doctor.diagnose_paths([tmp_path], root=tmp_path, schema_paths=[])
    payload = to_json(summary)['summary']['not_analysed']
    assert payload['call_sites'] == 1
    assert payload['files'] == 1
    assert payload['codes'] == {'opaque-where': 1, 'unparsed-file': 1}


@pytest.mark.usefixtures('installed')
def test_the_text_report_includes_schema_facts_when_they_are_available(tmp_path: Path) -> None:
    write(tmp_path, 'a.py', 'db.account.find_many()\n')
    diagnosis = _doctor.diagnose_paths([tmp_path], root=tmp_path, schema_paths=[])
    text = render_text(diagnosis)
    assert 'models,' in text
    assert 'relation shapes:' in text
    assert 'no verified translation path:' in text


@pytest.mark.usefixtures('installed')
def test_the_client_attribute_is_resolved_to_the_model_name() -> None:
    """`db.account` is `Account`; grouping by the attribute would name it wrong."""
    diagnosis = diagnose(
        scan_source('db = None\nx = db.account.find_many()\n', 'app.py'),
        schema=schema_facts([]),
    )
    assert [group.key for group in diagnosis.models] == ['Account']
    assert to_json(diagnosis)['call_sites'][0]['model'] == 'Account'


def test_every_action_the_scanner_finds_has_a_verdict() -> None:
    """A new action with no table entry would fall through to `verified`.

    That is the one failure mode the design cannot tolerate: claiming a
    translation exists because nothing said otherwise.
    """
    unclassified = sorted(
        action
        for action in ACTIONS
        if action not in _doctor.VERIFIED_ACTIONS
        and action not in _doctor.STOP_ACTIONS
        and action not in _doctor.UNKNOWN_ACTIONS
    )
    assert unclassified == []
    assert sorted(ACTIONS) == sorted(_doctor.COVERED_ARGUMENTS)


def test_a_client_matched_only_by_naming_convention_is_a_caveat(tmp_path: Path) -> None:
    """`db.user.count()` with no visible binding is weaker evidence, and says so."""
    (tmp_path / 'a.py').write_text('async def go():\n    await db.user.count()\n')
    diagnosis = _doctor.diagnose_paths([tmp_path], root=tmp_path, schema_paths=[])
    counts = {caveat.code: caveat.count for caveat in diagnosis.caveats}
    assert counts == {'convention-client': 1}
    assert [finding.status for finding in diagnosis.findings] == ['verified']
