"""What the AST scanner finds, and — as importantly — what it refuses to claim.

The runbook's §1 inventory is a set of greps. A grep cannot tell
`db.user.find_many(...)` from the same text inside a docstring, cannot follow a
client bound to any name other than the one in the pattern, and cannot see the
*arguments*, which is where half of §5 lives. This module pins the behaviour a
grep cannot have.
"""

from __future__ import annotations

from typing import List, Optional
from pathlib import Path
from textwrap import dedent

from prisma.sa._scan import CallSite, scan_paths, scan_source


def sites(source: str, path: str = 'app.py') -> List[CallSite]:
    return scan_source(dedent(source), path).call_sites


def actions(source: str) -> List[str]:
    return [site.action for site in sites(source)]


def one(source: str) -> CallSite:
    found = sites(source)
    # built unconditionally rather than inside the assert message: a
    # comprehension that only runs on failure leaves an unexecuted loop-exit arc
    described = [(site.action, site.receiver) for site in found]
    assert len(found) == 1, described
    return found[0]


def find(found: List[CallSite], action: str) -> CallSite:
    described = [site.action for site in found]
    matches = [site for site in found if site.action == action]
    assert len(matches) == 1, described
    return matches[0]


# --------------------------------------------------------------------------
# the three entry syntaxes
# --------------------------------------------------------------------------


def test_client_attribute_entry() -> None:
    site = one("""
        from prisma import Prisma

        db = Prisma()

        async def main() -> None:
            await db.user.find_many(take=5)
    """)
    assert site.entry == 'client'
    assert site.action == 'find_many'
    assert site.receiver == 'db.user'
    assert site.model == 'user'
    assert site.resolution == 'binding'
    assert site.line == 7


def test_model_prisma_entry() -> None:
    site = one("""
        from prisma.models import User

        async def main() -> None:
            await User.prisma().find_first(where={'id': 1})
    """)
    assert site.entry == 'model'
    assert site.model == 'User'
    assert site.action == 'find_first'
    assert site.receiver == 'User.prisma()'


def test_transaction_entries() -> None:
    found = sites("""
        from prisma import Prisma

        db = Prisma()

        async def main() -> None:
            async with db.tx() as tx:
                await tx.user.create(data={'name': 'a'})
            async with db.batch_() as batcher:
                batcher.user.create(data={'name': 'b'})
    """)
    assert [site.action for site in found] == ['tx', 'create', 'batch_', 'create']
    assert find(found, 'tx').entry == 'transaction'
    assert find(found, 'batch_').entry == 'transaction'


def test_client_bound_to_an_alias_is_still_found() -> None:
    """A regex keyed on `db.` misses this; the binding is what identifies it."""
    site = one("""
        from prisma import Prisma as PrismaClient

        connection = PrismaClient()

        async def main() -> None:
            await connection.post.delete_many()
    """)
    assert site.receiver == 'connection.post'
    assert site.resolution == 'binding'


def test_client_from_get_client() -> None:
    site = one("""
        from prisma import get_client

        async def main() -> None:
            handle = get_client()
            await handle.post.count()
    """)
    assert site.resolution == 'binding'
    assert site.action == 'count'


def test_client_from_an_annotation() -> None:
    site = one("""
        from prisma import Prisma

        async def handler(session: Prisma) -> None:
            await session.post.find_many()
    """)
    assert site.resolution == 'binding'
    assert site.receiver == 'session.post'


def test_client_on_self() -> None:
    site = one("""
        from prisma import Prisma

        class Repo:
            def __init__(self) -> None:
                self.database = Prisma()

            async def all(self) -> None:
                await self.database.post.find_many()
    """)
    assert site.receiver == 'self.database.post'
    assert site.model == 'post'


def test_transaction_handle_is_a_client() -> None:
    """`tx = await db.tx().start()` binds a client under a new name."""
    found = sites("""
        from prisma import Prisma

        db = Prisma()

        async def main() -> None:
            manager = db.tx()
            tx = await manager.start()
            await tx.user.update(where={'id': 1}, data={'name': 'x'})
    """)
    update = find(found, 'update')
    assert update.resolution == 'binding'
    assert update.receiver == 'tx.user'


# --------------------------------------------------------------------------
# what a grep gets wrong
# --------------------------------------------------------------------------


def test_strings_and_comments_are_not_call_sites() -> None:
    assert (
        actions("""
        from prisma import Prisma

        db = Prisma()

        DOCS = '''
        await db.user.find_many(take=5)
        '''

        # await db.user.delete_many()

        def helper() -> str:
            \"\"\"Example: await db.user.create(data={}).\"\"\"
            return DOCS
    """)
        == []
    )


def test_unrelated_methods_are_not_call_sites() -> None:
    """`update`, `count`, `create` and `delete` collide with the stdlib."""
    assert (
        actions("""
        def helper(mapping, items, path):
            mapping.update({'a': 1})
            items.count(3)
            path.parent.create()
            del mapping
    """)
        == []
    )


def test_a_distinctive_action_on_an_unresolved_receiver_is_recorded() -> None:
    """`find_many` is Prisma's; the receiver could not be resolved, so say so."""
    site = one("""
        async def handler(store):
            await store.user.find_many()
    """)
    assert site.resolution == 'unresolved'
    assert site.action == 'find_many'


def test_conventional_client_names_are_marked_as_such() -> None:
    """`db.user.count()` with no visible binding is a convention, not a fact."""
    site = one("""
        async def handler():
            await db.user.count()
    """)
    assert site.resolution == 'convention'


def test_dynamic_model_access_is_recorded_not_dropped() -> None:
    site = one("""
        from prisma import Prisma

        db = Prisma()

        async def handler(name: str):
            await getattr(db, name).find_many()
    """)
    assert site.resolution == 'dynamic'
    assert site.receiver == 'getattr(db, name)'


# --------------------------------------------------------------------------
# arguments
# --------------------------------------------------------------------------


def test_keyword_arguments_are_captured() -> None:
    site = one("""
        from prisma import Prisma

        db = Prisma()

        async def main():
            await db.user.find_many(where={'id': 1}, take=5)
    """)
    assert sorted(site.arguments) == ['take', 'where']
    assert site.opaque_arguments == []


def test_positional_arguments_are_named() -> None:
    """`update(data, where)` and `upsert(where, data)` disagree on order."""
    found = sites("""
        from prisma import Prisma

        db = Prisma()

        async def main():
            await db.user.update({'name': 'a'}, {'id': 1})
            await db.user.upsert({'id': 1}, {'create': {}, 'update': {}})
    """)
    update = find(found, 'update')
    assert sorted(update.arguments) == ['data', 'where']
    assert isinstance(update.arguments['where'], object)

    upsert = find(found, 'upsert')
    assert sorted(upsert.arguments) == ['data', 'where']


def test_double_star_arguments_are_opaque() -> None:
    site = one("""
        from prisma import Prisma

        db = Prisma()

        async def main(**kwargs):
            await db.user.find_many(**kwargs)
    """)
    assert site.opaque_arguments == ['**']


def test_extra_positional_arguments_are_opaque() -> None:
    """`query_raw(sql, *args)` takes more positionals than it names."""
    site = one("""
        from prisma import Prisma

        db = Prisma()

        async def main(value):
            await db.query_raw('SELECT 1 WHERE x = $1', value)
    """)
    assert site.action == 'query_raw'
    assert site.entry == 'client'
    assert site.opaque_arguments == ['*']


# --------------------------------------------------------------------------
# transaction blocks
# --------------------------------------------------------------------------


def test_call_sites_carry_their_enclosing_transaction_block() -> None:
    found = sites("""
        from prisma import Prisma

        db = Prisma()

        async def main():
            async with db.tx() as tx:
                await tx.user.create(data={'name': 'a'})
            await db.user.create(data={'name': 'b'})
    """)
    inside = [site for site in found if site.block is not None]
    outside = [site for site in found if site.block is None]
    assert len(inside) == 2  # the `tx()` itself, and the create in its body
    assert len(outside) == 1
    assert {site.block for site in inside} == {'tx@app.py:7'}


def test_a_transaction_without_a_with_block_has_no_visible_body() -> None:
    found = sites("""
        from prisma import Prisma

        db = Prisma()

        async def main():
            tx = await db.tx().start()
            await tx.user.create(data={'name': 'a'})
    """)
    assert find(found, 'tx').block is None
    assert find(found, 'create').block is None


# --------------------------------------------------------------------------
# files
# --------------------------------------------------------------------------


def test_scan_paths_walks_a_tree(tmp_path: Path) -> None:
    (tmp_path / 'pkg').mkdir()
    (tmp_path / 'pkg' / 'a.py').write_text('import prisma\ndb = prisma.Prisma()\nx = db.user.find_many()\n')
    (tmp_path / 'pkg' / 'notes.txt').write_text('db.user.find_many()')
    (tmp_path / 'pkg' / '__pycache__').mkdir()
    (tmp_path / 'pkg' / '__pycache__' / 'b.py').write_text('x = 1\n')

    result = scan_paths([tmp_path], root=tmp_path)
    assert result.files_scanned == 1
    assert [site.path for site in result.call_sites] == ['pkg/a.py']


def test_scan_paths_accepts_a_single_file(tmp_path: Path) -> None:
    target = tmp_path / 'a.py'
    target.write_text('db = None\nx = db.user.find_many()\n')
    result = scan_paths([target], root=tmp_path)
    assert result.files_scanned == 1
    assert [site.path for site in result.call_sites] == ['a.py']


def test_a_file_that_does_not_parse_is_reported_not_skipped(tmp_path: Path) -> None:
    (tmp_path / 'broken.py').write_text('def (:\n')
    result = scan_paths([tmp_path], root=tmp_path)
    assert [entry.path for entry in result.unparsed] == ['broken.py']
    assert 'invalid syntax' in result.unparsed[0].error


def test_a_file_that_cannot_be_decoded_is_reported(tmp_path: Path) -> None:
    (tmp_path / 'binary.py').write_bytes(b'x = "\xff\xfe"\n')
    result = scan_paths([tmp_path], root=tmp_path)
    assert [entry.path for entry in result.unparsed] == ['binary.py']


def test_a_path_outside_the_root_keeps_its_absolute_name(tmp_path: Path) -> None:
    outside = tmp_path / 'outside.py'
    outside.write_text('db = None\nx = db.user.find_many()\n')
    result = scan_paths([outside], root=tmp_path / 'root')
    assert [site.path for site in result.call_sites] == [outside.as_posix()]


def test_render_covers_the_shapes_it_meets() -> None:
    """Receiver rendering has no `ast.unparse` to fall back on before 3.9."""
    found = sites("""
        from prisma import Prisma

        db = Prisma()
        clients = {'a': db}

        async def main():
            await clients['a'].user.find_many()
            await getattr(db, 'user').find_many()
    """)
    receivers = [site.receiver for site in found]
    assert receivers == ['clients[...].user', "getattr(db, 'user')"]


def test_call_site_argument_nodes_are_ast_nodes() -> None:
    site = one("""
        from prisma import Prisma

        db = Prisma()

        async def main():
            await db.user.create(data={'name': 'a'})
    """)
    node: Optional[object] = site.arguments.get('data')
    assert node is not None
    assert node.__class__.__name__ == 'Dict'


def test_model_prisma_entry_through_the_models_module() -> None:
    """`models.User.prisma()` is as common as importing the class."""
    site = one("""
        from prisma import models

        async def main() -> None:
            await models.User.prisma().find_many()
    """)
    assert site.entry == 'model'
    assert site.model == 'User'
    assert site.receiver == 'models.User.prisma()'


def test_a_lowercase_owner_is_not_a_model_accessor() -> None:
    """`self.prisma()` is a method call, not `Model.prisma()`."""
    assert actions("""
        class Repo:
            def go(self):
                return self.prisma().find_many()
    """) == ['find_many']


def test_a_computed_owner_is_not_a_model_accessor() -> None:
    assert actions("""
        registry = {}

        def go():
            return registry['User'].prisma().find_many()
    """) == ['find_many']


def test_the_outer_client_used_inside_a_block_is_not_a_member_of_it() -> None:
    """`async with db.tx() as tx` makes `tx` transactional and leaves `db` alone.

    Attributing a `db.` call written inside the block to the block would report
    a verified call site as blocked by a STOP it never shared a transaction with.
    """
    found = sites("""
        from prisma import Prisma

        db = Prisma()

        async def main():
            async with db.tx() as tx:
                await tx.user.create(data={'name': 'a'})
                await db.user.count()
    """)
    assert find(found, 'create').block == 'tx@app.py:7'
    assert find(found, 'count').block is None


def test_model_prisma_joins_a_block_through_the_handle_it_is_passed() -> None:
    found = sites("""
        from prisma import Prisma
        from prisma.models import User

        db = Prisma()

        async def main():
            async with db.tx() as tx:
                await User.prisma(tx).create(data={'name': 'a'})
                await User.prisma().create(data={'name': 'b'})
    """)
    joined = [site for site in found if site.action == 'create' and site.block is not None]
    detached = [site for site in found if site.action == 'create' and site.block is None]
    assert [site.receiver for site in joined] == ['User.prisma(tx)']
    assert [site.receiver for site in detached] == ['User.prisma()']


def test_a_block_that_binds_nothing_has_no_members() -> None:
    """`async with db.tx():` hands the transactional client to nobody."""
    found = sites("""
        from prisma import Prisma

        db = Prisma()

        async def main():
            async with db.tx():
                await db.user.count()
    """)
    assert find(found, 'tx').block == 'tx@app.py:7'
    assert find(found, 'count').block is None
