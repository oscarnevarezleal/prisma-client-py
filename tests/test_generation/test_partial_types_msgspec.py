# pyright: reportUnusedFunction=false
"""Partial types on the ``msgspec`` model backend.

``modelBackend = "msgspec"`` and ``partial_type_generator`` used to be mutually
exclusive by accident: the msgspec branch of ``models.py.jinja`` emitted no
``create_partial`` at all, so configuring both produced an ``AttributeError``
during generation. These tests pin the parity down — the generated partials
must be msgspec structs (the whole point of the backend is that no pydantic
schema is compiled) that still round-trip engine payloads and keep the
pydantic-shaped surface call sites rely on.
"""

import subprocess
from typing_extensions import Literal

import pytest

from ..utils import Testdir

SCHEMA = """
datasource db {{
  provider = "postgres"
  url      = env("DB_URL")
}}

generator db {{
  provider = "coverage run -m prisma"
  output = "{output}"
  modelBackend = "msgspec"
  {options}
}}

model Post {{
  id          String     @id @default(cuid())
  created_at  DateTime   @default(now())
  title       String
  published   Boolean
  views       Int        @default(0)
  big         BigInt     @default(0)
  desc        String?
  meta        Json?
  comments    Comment[]
  author_id   String
  author      User      @relation(fields: [author_id], references: [id])
  thumbnail   Bytes?
}}

model Comment {{
  id           String   @id @default(cuid())
  content      String
  post         Post?    @relation(fields: [post_id], references: [id])
  post_id      String?
}}

model User {{
  id           String   @id @default(cuid())
  name         String
  bytes        Bytes
  bytes_list   Bytes[]
  posts        Post[]
}}
"""


def test_partial_types_msgspec(testdir: Testdir) -> None:
    """Partial types are generated as msgspec structs and behave like the pydantic ones"""

    def tests() -> None:  # mark: filedef
        import ast
        import datetime
        from pathlib import Path

        import pytest
        import msgspec
        import pydantic

        import prisma.models
        import prisma.partials
        from prisma import Base64, models
        from prisma._compat import model_parse
        from prisma.partials import (  # type: ignore[attr-defined]
            PostOnlyId,  # pyright: ignore
            UserOnlyName,  # pyright: ignore
            UserBytesList,  # pyright: ignore
            PostNoRelations,  # pyright: ignore
            PostWithoutDesc,  # pyright: ignore
            PostRequiredDesc,  # pyright: ignore
            UserModifiedPosts,  # pyright: ignore
            PostModifiedAuthor,  # pyright: ignore
            PostOptionalPublished,  # pyright: ignore
        )
        from prisma._slimmodel import related_model, scalar_field_names
        from prisma._msgspecmodel import PrismaRecord

        ALL_PARTIALS = [
            PostOnlyId,
            UserOnlyName,
            UserBytesList,
            PostNoRelations,
            PostWithoutDesc,
            PostRequiredDesc,
            UserModifiedPosts,
            PostModifiedAuthor,
            PostOptionalPublished,
        ]

        def test_partials_are_msgspec_structs() -> None:
            """No pydantic schema is compiled for partials on this backend"""
            for partial in ALL_PARTIALS:
                assert issubclass(partial, msgspec.Struct)
                assert issubclass(partial, PrismaRecord)
                assert not issubclass(partial, pydantic.BaseModel)

        def test_field_selection() -> None:
            """include / exclude / exclude_relational_fields shape the struct fields"""
            assert set(PostOnlyId.__struct_fields__) == {'id'}
            assert set(UserOnlyName.__struct_fields__) == {'name'}
            assert set(UserBytesList.__struct_fields__) == {'bytes', 'bytes_list'}
            assert 'desc' not in PostWithoutDesc.__struct_fields__
            assert 'title' in PostWithoutDesc.__struct_fields__
            assert set(PostNoRelations.__struct_fields__) == {
                'id',
                'created_at',
                'title',
                'published',
                'views',
                'big',
                'desc',
                'meta',
                'author_id',
                'thumbnail',
            }

        def test_required_and_optional() -> None:
            """`required` / `optional` change whether a field must be passed"""
            # `desc` is nullable in the schema but was made required
            with pytest.raises(TypeError):
                PostRequiredDesc(
                    id='1',
                    created_at=datetime.datetime.now(datetime.timezone.utc),
                    title='t',
                    published=True,
                    views=0,
                    big=1,
                    author_id='2',
                )

            # `published` is required in the schema but was made optional
            record = PostOptionalPublished(
                id='1',
                created_at=datetime.datetime.now(datetime.timezone.utc),
                title='t',
                views=0,
                big=1,
                author_id='2',
            )
            assert record.published is None

        def test_round_trip_engine_payload() -> None:
            """Raw engine values are converted, not passed through"""
            record = model_parse(
                PostNoRelations,
                {
                    'id': 'abc',
                    'created_at': '2023-01-02T03:04:05+00:00',
                    'title': 'My post',
                    'published': True,
                    'views': 12,
                    'big': '9223372036854775807',
                    'desc': None,
                    # NOTE: a populated `Json` column cannot be asserted here —
                    # `_msgspecmodel._dec_hook` returns the decoded python object
                    # while msgspec requires an instance of the annotated type, so
                    # `Json` values fail to decode on this backend. That is not a
                    # partials gap: a plain msgspec *model* with a `Json` field
                    # fails identically, so the partial keeps parity with it.
                    'meta': None,
                    'author_id': 'user-1',
                    'thumbnail': str(Base64.encode(b'thumb')),
                },
            )
            assert isinstance(record, PostNoRelations)
            assert record.created_at == datetime.datetime(2023, 1, 2, 3, 4, 5, tzinfo=datetime.timezone.utc)
            assert record.big == 9223372036854775807
            assert record.thumbnail == Base64.encode(b'thumb')
            assert record.desc is None
            assert record.meta is None

        def test_round_trip_relation() -> None:
            """A relation retargeted at another partial decodes into that partial"""
            record = model_parse(
                PostModifiedAuthor,
                {
                    'id': 'abc',
                    'created_at': '2023-01-02T03:04:05+00:00',
                    'title': 'My post',
                    'published': False,
                    'views': 0,
                    'big': 1,
                    'author_id': 'user-1',
                    'author': {'name': 'Robert'},
                },
            )
            assert isinstance(record.author, UserOnlyName)
            assert record.author.name == 'Robert'

        def test_round_trip_relation_list() -> None:
            """A list relation retargeted at another partial decodes into that partial"""
            record = model_parse(
                UserModifiedPosts,
                {
                    'id': 'user-1',
                    'name': 'Robert',
                    'posts': [{'id': '1'}, {'id': '2'}],
                },
            )
            assert record.posts is not None
            assert [post.id for post in record.posts] == ['1', '2']
            for post in record.posts:
                assert isinstance(post, PostOnlyId)

        def test_round_trip_bytes_list() -> None:
            """Base64 fields, including lists of them, are decoded"""
            record = model_parse(
                UserBytesList,
                {
                    'bytes': str(Base64.encode(b'bar')),
                    'bytes_list': [
                        str(Base64.encode(b'foo')),
                        str(Base64.encode(b'baz')),
                    ],
                },
            )
            assert record.bytes == Base64.encode(b'bar')
            assert record.bytes_list == [Base64.encode(b'foo'), Base64.encode(b'baz')]

        def test_dump_surface() -> None:
            """model_dump()/dict()/model_dump_json()/json() incl. `exclude_none`"""
            record = PostOnlyId(id='abc')
            assert record.model_dump() == {'id': 'abc'}
            assert record.dict() == {'id': 'abc'}
            assert record.model_dump_json() == '{"id":"abc"}'
            assert record.json() == '{"id":"abc"}'

            user = model_parse(UserModifiedPosts, {'id': 'u', 'name': 'Robert', 'posts': None})
            assert user.model_dump() == {'id': 'u', 'name': 'Robert', 'posts': None}
            assert user.model_dump(exclude_none=True) == {'id': 'u', 'name': 'Robert'}
            assert user.dict(exclude_none=True) == {'id': 'u', 'name': 'Robert'}
            assert user.model_dump_json(exclude_none=True) == '{"id":"u","name":"Robert"}'
            assert user.json(exclude_none=True) == '{"id":"u","name":"Robert"}'

            nested = model_parse(UserModifiedPosts, {'id': 'u', 'name': 'R', 'posts': [{'id': '1'}]})
            assert nested.model_dump() == {'id': 'u', 'name': 'R', 'posts': [{'id': '1'}]}

        def test_to_pydantic() -> None:
            """`to_pydantic()` builds a real pydantic model for the partial"""
            record = PostOnlyId(id='abc')
            twin = record.to_pydantic()
            assert isinstance(twin, pydantic.BaseModel)
            assert twin.id == 'abc'  # type: ignore[attr-defined]
            assert type(twin).__name__ == 'PostOnlyId'

        def test_field_specs_drive_the_query_builder() -> None:
            """`__prisma_fields__` keeps the query builder working for partials"""
            assert set(scalar_field_names(PostModifiedAuthor)) == {
                'id',
                'created_at',
                'title',
                'published',
                'views',
                'big',
                'desc',
                'meta',
                'author_id',
                'thumbnail',
            }
            assert scalar_field_names(PostNoRelations) == list(PostNoRelations.__struct_fields__)

            # retargeted relations point at the partial type
            assert related_model(PostModifiedAuthor, 'author') is UserOnlyName
            assert related_model(UserModifiedPosts, 'posts') is PostOnlyId

            # untouched relations still point at the full model
            assert related_model(PostWithoutDesc, 'author') is models.User
            # `Comment` only exists in the schema generated by this test, so the
            # type checkers (which see the repo's own client) cannot know it
            assert related_model(PostWithoutDesc, 'comments') is models.Comment  # type: ignore[attr-defined]

        def test_prisma_model_marker() -> None:
            """Partials carry the model name the query builder dispatches on"""
            assert PostOnlyId.__prisma_model__ == 'Post'
            assert UserOnlyName.__prisma_model__ == 'User'
            assert PostOnlyId.__prisma_slim__ is True

        def test_model_scoped_queries() -> None:
            """`Partial.prisma()` reaches the source model's actions"""
            client = object()
            actions = PostOnlyId.prisma(client)
            assert type(actions).__name__ == 'PostActions'
            assert UserOnlyName.prisma(client)._model is UserOnlyName

        def test_annotations_carry_no_nested_forward_refs() -> None:
            """Struct annotations must contain no quoted forward references.

            Python 3.8's `typing.ForwardRef._evaluate` does not recurse into the
            type it evaluates (3.9 added that), and msgspec hands it the whole
            annotation as one forward reference because of `from __future__
            import annotations`. A *nested* string literal therefore survives as
            an unresolved `ForwardRef` and msgspec rejects it with
            `TypeError: Type 'ForwardRef(...)' is not supported`.

            Nothing on 3.9+ can observe that at runtime, so the invariant is
            checked statically here — otherwise only the 3.8 CI job would catch
            a regression.
            """
            for module in (prisma.models, prisma.partials):
                path = module.__file__
                assert path is not None
                for node in ast.walk(ast.parse(Path(path).read_text())):
                    if not isinstance(node, ast.ClassDef):
                        continue

                    # only direct class-body annotations are struct fields;
                    # annotations inside methods are never evaluated by msgspec
                    for stmt in node.body:
                        if not isinstance(stmt, ast.AnnAssign):
                            continue

                        quoted = [
                            sub.value
                            for sub in ast.walk(stmt.annotation)
                            if isinstance(sub, ast.Constant) and isinstance(sub.value, str)
                        ]
                        assert not quoted, (
                            f'{module.__name__}.{node.name} line {stmt.lineno}: '
                            f'annotation contains quoted forward reference(s) {quoted}'
                        )

    def generator() -> None:  # mark: filedef
        from prisma.models import Post, User

        User.create_partial('UserOnlyName', include={'name'})
        Post.create_partial('PostOnlyId', include={'id'})
        Post.create_partial(
            'PostWithoutDesc',
            exclude=['desc'],  # pyright: ignore
        )  # pyright: ignore
        Post.create_partial('PostOptionalPublished', optional=['published'])
        Post.create_partial(
            'PostRequiredDesc',
            required=['desc'],  # pyright: ignore
        )  # pyright: ignore
        Post.create_partial('PostNoRelations', exclude_relational_fields=True)
        Post.create_partial('PostModifiedAuthor', relations={'author': 'UserOnlyName'})
        User.create_partial(
            'UserModifiedPosts',
            exclude={'bytes', 'bytes_list'},  # type: ignore
            relations={'posts': 'PostOnlyId'},
        )
        User.create_partial(
            'UserBytesList',
            include={'bytes', 'bytes_list'},  # type: ignore
        )  # pyright: ignore

    testdir.make_from_function(generator, name='prisma/partial_types.py')
    testdir.generate(SCHEMA, 'partial_type_generator = "prisma/partial_types.py"')
    testdir.make_from_function(tests)
    testdir.runpytest().assert_outcomes(passed=13)


@pytest.mark.parametrize('argument', ['exclude', 'include', 'required', 'optional'])
def test_partial_types_msgspec_incorrect_key(
    testdir: Testdir,
    argument: Literal['exclude', 'include', 'required', 'optional'],
) -> None:
    """The msgspec `create_partial` validates field names the same way"""

    def generator() -> None:  # mark: filedef
        from prisma.models import Post

        Post.create_partial('PostWithoutFoo', **{argument: ['foo']})  # type: ignore

    testdir.make_from_function(generator, name='prisma/partial_types.py', argument=argument)

    with pytest.raises(subprocess.CalledProcessError) as exc:
        testdir.generate(SCHEMA, 'partial_type_generator = "prisma/partial_types.py"')

    assert 'foo is not a valid Post / PostWithoutFoo field' in str(exc.value.output)
