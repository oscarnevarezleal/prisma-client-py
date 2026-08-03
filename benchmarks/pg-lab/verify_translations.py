"""Check each Prisma -> SQLAlchemy Core translation against a live database.

`docs/prisma-to-sqlalchemy-runbook.md` is written to be followed by an agent
with no judgement of its own, so nothing may go in it that has not been run.
Every row of the runbook's translation table is a case here: the Prisma call and
the SQLAlchemy call are executed against the same rows and their results
compared. A case that does not match does not get documented — it gets listed as
a STOP.

    BENCH_DATABASE_URL=postgresql://... python verify_translations.py --workdir /tmp/pglab-sa

Exit code is the number of mismatches.
"""

from __future__ import annotations

import os
import sys
import argparse
import importlib
from typing import Any, Dict, List, Tuple

import sqlalchemy as sa

# (name, prisma call, sqlalchemy call, expectation). `mismatch` cases pin a
# translation that is *wrong*: if one of those ever starts matching, the case has
# gone vacuous and stops protecting anything.
# (name, prisma call, sqlalchemy call) and optionally an expectation. The
# default is 'match'. A 'mismatch' case pins a translation that is *wrong*: if
# one of those ever starts matching, the case has gone vacuous — the data no
# longer exercises the trap — and it stops protecting anything.
Case = Tuple[Any, ...]


def normalize(value: Any) -> Any:
    """Compare on values, not on the container the two APIs happen to use."""
    if isinstance(value, sa.engine.Row):
        return tuple(value)
    if isinstance(value, sa.RowMapping):
        return dict(value)
    if isinstance(value, (list, tuple)):
        return [normalize(item) for item in value]
    if hasattr(value, 'model_dump'):
        return {k: normalize(v) for k, v in value.model_dump().items() if not isinstance(v, (list, dict))}
    return value


def setup_3vl(conn: sa.Connection) -> None:
    """Give post `p1` a child whose filter predicate evaluates to NULL.

    Without this the `every` cases are vacuous — every comment in the seed data
    has a NULL `parentId`, so the correct and incorrect translations agree and
    the case proves nothing.
    """
    conn.execute(
        sa.text('DELETE FROM "Reaction" WHERE "commentId" IN (SELECT id FROM "Comment" WHERE "postId" = \'p1\')')
    )
    conn.execute(sa.text('DELETE FROM "Comment" WHERE "postId" = \'p1\''))
    conn.execute(
        sa.text(
            'INSERT INTO "Comment"(id,"postId","authorId","parentId",body,"isHidden","createdAt") '
            "VALUES ('cB','p1','u1',NULL,'b',false,now())"
        )
    )
    conn.execute(
        sa.text(
            'INSERT INTO "Comment"(id,"postId","authorId","parentId",body,"isHidden","createdAt") '
            "VALUES ('cA','p1','u1','cB','a',false,now())"
        )
    )
    conn.commit()


def build_cases(db: Any, conn: sa.Connection, md: sa.MetaData) -> List[Case]:
    post = md.tables['Post']
    user = md.tables['User']
    comment = md.tables['Comment']
    tag = md.tables['Tag']
    post_tag = md.tables['PostTag']

    def ids(rows: Any) -> List[str]:
        return sorted(r['id'] if isinstance(r, (dict, sa.RowMapping)) else r.id for r in rows)

    cases: List[Case] = [
        # -- reads -------------------------------------------------------------
        (
            'find_unique by id',
            lambda: db.post.find_unique(where={'id': 'p1'}).title,
            lambda: conn.execute(sa.select(post.c.title).where(post.c.id == 'p1')).scalar_one(),
        ),
        (
            'find_unique by compound unique',
            lambda: db.post.find_unique(where={'siteId_slug': {'siteId': 's2', 'slug': 'post-1'}}).id,
            lambda: conn.execute(
                sa.select(post.c.id).where(sa.and_(post.c.siteId == 's2', post.c.slug == 'post-1'))
            ).scalar_one(),
            'match',
        ),
        (
            'find_first with order',
            lambda: db.post.find_first(order={'createdAt': 'desc'}).id,
            lambda: conn.execute(sa.select(post.c.id).order_by(post.c.createdAt.desc()).limit(1)).scalar_one(),
        ),
        (
            'find_many take/skip/order',
            lambda: ids(db.post.find_many(take=5, skip=10, order={'id': 'asc'})),
            lambda: ids(conn.execute(sa.select(post).order_by(post.c.id.asc()).limit(5).offset(10)).mappings()),
        ),
        (
            'where equals',
            lambda: ids(db.post.find_many(where={'status': 'PUBLISHED'}, take=20, order={'id': 'asc'})),
            lambda: ids(
                conn.execute(
                    sa.select(post).where(post.c.status == 'PUBLISHED').order_by(post.c.id.asc()).limit(20)
                ).mappings()
            ),
        ),
        (
            'where in',
            lambda: ids(db.post.find_many(where={'id': {'in': ['p1', 'p2', 'p3']}}, order={'id': 'asc'})),
            lambda: ids(
                conn.execute(
                    sa.select(post).where(post.c.id.in_(['p1', 'p2', 'p3'])).order_by(post.c.id.asc())
                ).mappings()
            ),
        ),
        (
            'where not',
            lambda: ids(db.post.find_many(where={'status': {'not': 'PUBLISHED'}}, take=20, order={'id': 'asc'})),
            lambda: ids(
                conn.execute(
                    sa.select(post).where(post.c.status != 'PUBLISHED').order_by(post.c.id.asc()).limit(20)
                ).mappings()
            ),
        ),
        (
            'where contains',
            lambda: ids(db.post.find_many(where={'title': {'contains': 'Post 1'}}, order={'id': 'asc'})),
            lambda: ids(
                conn.execute(sa.select(post).where(post.c.title.like('%Post 1%')).order_by(post.c.id.asc())).mappings()
            ),
        ),
        (
            'where startsWith',
            lambda: ids(db.post.find_many(where={'slug': {'startsWith': 'post-1'}}, order={'id': 'asc'})),
            lambda: ids(
                conn.execute(sa.select(post).where(post.c.slug.like('post-1%')).order_by(post.c.id.asc())).mappings()
            ),
        ),
        (
            'where contains insensitive',
            lambda: ids(
                db.post.find_many(where={'title': {'contains': 'post 1', 'mode': 'insensitive'}}, order={'id': 'asc'})
            ),
            lambda: ids(
                conn.execute(sa.select(post).where(post.c.title.ilike('%post 1%')).order_by(post.c.id.asc())).mappings()
            ),
        ),
        (
            'where null',
            lambda: ids(db.post.find_many(where={'publishedAt': None}, take=20, order={'id': 'asc'})),
            lambda: ids(
                conn.execute(
                    sa.select(post).where(post.c.publishedAt.is_(None)).order_by(post.c.id.asc()).limit(20)
                ).mappings()
            ),
        ),
        (
            'where not null',
            lambda: ids(db.post.find_many(where={'publishedAt': {'not': None}}, take=20, order={'id': 'asc'})),
            lambda: ids(
                conn.execute(
                    sa.select(post).where(post.c.publishedAt.is_not(None)).order_by(post.c.id.asc()).limit(20)
                ).mappings()
            ),
        ),
        (
            'where AND',
            lambda: ids(
                db.post.find_many(
                    where={'AND': [{'status': 'PUBLISHED'}, {'siteId': 's1'}]}, take=20, order={'id': 'asc'}
                )
            ),
            lambda: ids(
                conn.execute(
                    sa.select(post)
                    .where(sa.and_(post.c.status == 'PUBLISHED', post.c.siteId == 's1'))
                    .order_by(post.c.id.asc())
                    .limit(20)
                ).mappings()
            ),
        ),
        (
            'where OR',
            lambda: ids(
                db.post.find_many(where={'OR': [{'id': 'p1'}, {'id': 'p2'}]}, order={'id': 'asc'}),
            ),
            lambda: ids(
                conn.execute(
                    sa.select(post).where(sa.or_(post.c.id == 'p1', post.c.id == 'p2')).order_by(post.c.id.asc())
                ).mappings()
            ),
        ),
        (
            'where NOT',
            lambda: ids(db.post.find_many(where={'NOT': [{'status': 'PUBLISHED'}]}, take=20, order={'id': 'asc'})),
            lambda: ids(
                conn.execute(
                    sa.select(post).where(sa.not_(post.c.status == 'PUBLISHED')).order_by(post.c.id.asc()).limit(20)
                ).mappings()
            ),
        ),
        (
            'order by two columns',
            lambda: [(r.status, r.id) for r in db.post.find_many(take=10, order=[{'status': 'asc'}, {'id': 'desc'}])],
            lambda: [
                (r['status'], r['id'])
                for r in conn.execute(
                    sa.select(post).order_by(post.c.status.asc(), post.c.id.desc()).limit(10)
                ).mappings()
            ],
        ),
        (
            'distinct',
            lambda: sorted(r.status for r in db.post.find_many(distinct=['status'])),
            lambda: sorted(conn.execute(sa.select(post.c.status).distinct()).scalars()),
        ),
        # -- aggregates --------------------------------------------------------
        (
            'count',
            lambda: db.post.count(),
            lambda: conn.execute(sa.select(sa.func.count()).select_from(post)).scalar_one(),
        ),
        (
            'count with where',
            lambda: db.comment.count(where={'isHidden': False}),
            lambda: conn.execute(
                sa.select(sa.func.count()).select_from(comment).where(comment.c.isHidden.is_(False))
            ).scalar_one(),
        ),
        (
            'group_by with count',
            lambda: sorted((r['status'], r['_count']['id']) for r in db.post.group_by(['status'], count={'id': True})),
            lambda: sorted(
                (r['status'], r['n'])
                for r in conn.execute(
                    sa.select(post.c.status, sa.func.count(post.c.id).label('n')).group_by(post.c.status)
                ).mappings()
            ),
        ),
        # -- relation filters --------------------------------------------------
        (
            'relation some',
            lambda: ids(db.post.find_many(where={'comments': {'some': {'isHidden': False}}}, order={'id': 'asc'})),
            lambda: ids(
                conn.execute(
                    sa.select(post)
                    .where(sa.exists().where(sa.and_(comment.c.postId == post.c.id, comment.c.isHidden.is_(False))))
                    .order_by(post.c.id.asc())
                ).mappings()
            ),
        ),
        (
            'relation none',
            lambda: ids(db.post.find_many(where={'comments': {'none': {'isHidden': False}}}, order={'id': 'asc'})),
            lambda: ids(
                conn.execute(
                    sa.select(post)
                    .where(~sa.exists().where(sa.and_(comment.c.postId == post.c.id, comment.c.isHidden.is_(False))))
                    .order_by(post.c.id.asc())
                ).mappings()
            ),
        ),
        # `every` where the child predicate can evaluate to NULL. `setup_3vl`
        # gives post `p1` two comments: one satisfying the predicate, one whose
        # `parentId` is NULL so the predicate is UNKNOWN.
        #
        # Prisma counts the UNKNOWN child as *not* a violation, so `p1` matches.
        # That is the `NOT (...)` reading. The set-theoretic reading — a child
        # not shown to satisfy C has not satisfied it — is `IS NOT TRUE`, and it
        # excludes `p1`. Migration targets Prisma's behaviour, not set theory.
        (
            'relation every, NULL-valued predicate: NOT(...) matches Prisma',
            lambda: 'p1' in ids(db.post.find_many(where={'comments': {'every': {'parentId': 'cB'}}})),
            lambda: 'p1'
            in ids(
                conn.execute(
                    sa.select(post).where(
                        ~sa.exists().where(sa.and_(comment.c.postId == post.c.id, sa.not_(comment.c.parentId == 'cB')))
                    )
                ).mappings()
            ),
            'match',
        ),
        (
            'relation every, IS NOT TRUE does NOT match Prisma',
            lambda: 'p1' in ids(db.post.find_many(where={'comments': {'every': {'parentId': 'cB'}}})),
            lambda: 'p1'
            in ids(
                conn.execute(
                    sa.select(post).where(
                        ~sa.exists().where(
                            sa.and_(comment.c.postId == post.c.id, (comment.c.parentId == 'cB').isnot(True))
                        )
                    )
                ).mappings()
            ),
            'mismatch',
        ),
        (
            'to-one relation filter',
            lambda: ids(
                db.post.find_many(
                    where={'author': {'is': {'handle': {'startsWith': 'handle1'}}}}, take=20, order={'id': 'asc'}
                )
            ),
            lambda: ids(
                conn.execute(
                    sa.select(post)
                    .where(sa.exists().where(sa.and_(user.c.id == post.c.authorId, user.c.handle.like('handle1%'))))
                    .order_by(post.c.id.asc())
                    .limit(20)
                ).mappings()
            ),
            'match',
        ),
        # -- includes ----------------------------------------------------------
        (
            'include to-one',
            lambda: [
                (p.id, p.author.email) for p in db.post.find_many(take=5, include={'author': True}, order={'id': 'asc'})
            ],
            lambda: [
                (r['id'], r['email'])
                for r in conn.execute(
                    sa.select(post.c.id, user.c.email)
                    .join(user, user.c.id == post.c.authorId)
                    .order_by(post.c.id.asc())
                    .limit(5)
                ).mappings()
            ],
        ),
        (
            'include to-many child count',
            lambda: sorted(
                (p.id, len(p.comments or []))
                for p in db.post.find_many(take=5, include={'comments': True}, order={'id': 'asc'})
            ),
            lambda: sorted(
                (r['id'], r['n'])
                for r in conn.execute(
                    sa.select(post.c.id, sa.func.count(comment.c.id).label('n'))
                    .select_from(post.outerjoin(comment, comment.c.postId == post.c.id))
                    .where(post.c.id.in_(sa.select(post.c.id).order_by(post.c.id.asc()).limit(5).scalar_subquery()))
                    .group_by(post.c.id)
                ).mappings()
            ),
        ),
        (
            'traverse an explicit m2m join model',
            lambda: sorted(t.tag.name for t in db.posttag.find_many(where={'postId': 'p1'}, include={'tag': True})),
            lambda: sorted(
                conn.execute(
                    sa.select(tag.c.name)
                    .select_from(post_tag.join(tag, tag.c.id == post_tag.c.tagId))
                    .where(post_tag.c.postId == 'p1')
                ).scalars()
            ),
        ),
    ]
    return cases


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--workdir', required=True)
    parser.add_argument('--package', default='pkg_sync')
    args = parser.parse_args()

    url = os.environ['BENCH_DATABASE_URL']
    os.chdir(args.workdir)
    sys.path.insert(0, os.getcwd())

    mod = importlib.import_module(args.package)
    db = mod.Prisma()
    db.connect()

    meta = importlib.import_module(f'{args.package}.metadata')
    from prisma.sa import build_metadata

    md = build_metadata(meta.SCHEMA, meta.ENUM_SCHEMA, meta.DATABASE_PROVIDER)
    engine = sa.create_engine(url.replace('postgresql://', 'postgresql+psycopg://'))
    conn = engine.connect()

    setup_3vl(conn)

    results: Dict[str, str] = {}
    for case in build_cases(db, conn, md):
        name, prisma_call, alchemy_call = case[0], case[1], case[2]
        expect = case[3] if len(case) > 3 else 'match'
        try:
            left = normalize(prisma_call())
        except Exception as exc:  # noqa: BLE001
            results[name] = f'PRISMA-ERROR {type(exc).__name__}: {exc}'
            continue
        try:
            right = normalize(alchemy_call())
        except Exception as exc:  # noqa: BLE001
            results[name] = f'SA-ERROR {type(exc).__name__}: {exc}'
            continue

        matched = left == right
        if expect == 'match':
            results[name] = 'MATCH' if matched else f'FAIL differs\n    prisma={left!r}\n    sa    ={right!r}'
        else:
            # the point of these is that they must NOT agree
            results[name] = f'FAIL agreed, so the case proves nothing: {left!r}' if matched else 'DIFFERS-AS-EXPECTED'

    width = max(len(name) for name in results)
    failures = 0
    for name, outcome in results.items():
        status = outcome.split()[0]
        if status == 'FAIL' or status.endswith('ERROR'):
            failures += 1
        print(f'{status:9} {name:<{width}}  {outcome[len(status):].strip()}')

    print(f'\n{len(results) - failures}/{len(results)} verified')

    conn.close()
    engine.dispose()
    db.disconnect()
    sys.exit(failures)


if __name__ == '__main__':
    main()
