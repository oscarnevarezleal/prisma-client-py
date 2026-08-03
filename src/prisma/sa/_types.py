"""Prisma scalar types as the database actually spells them, per provider.

Every entry here was checked against a database built by `prisma db push`, not
against the Prisma documentation. The two disagree in places that matter — a
`DateTime` is `timestamp(3)`, not `timestamp`, and getting the precision wrong
is a column alteration on every single migration.

Only providers verified that way are listed. A provider that is merely plausible
is worse than a missing one: the failure mode is a silent schema diff on someone
else's database, discovered during a deploy.
"""

from __future__ import annotations

from typing import Any, Dict, List, Callable, Optional

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

__all__ = (
    'SUPPORTED_PROVIDERS',
    'native_type',
    'UnsupportedProviderError',
    'check_provider',
    'scalar_type',
    'enum_type',
    'array_type',
)

#: Providers whose mapping has been verified against a real `prisma db push`.
SUPPORTED_PROVIDERS = frozenset({'postgresql'})


class UnsupportedProviderError(NotImplementedError):
    def __init__(self, provider: str) -> None:
        self.provider = provider
        super().__init__(
            f'Building SQLAlchemy metadata is not supported for provider {provider!r}.\n'
            f'  Verified providers: {", ".join(sorted(SUPPORTED_PROVIDERS))}.\n'
            '  Adding one means a scalar type table checked against a database built by\n'
            '  `prisma db push`, plus its constraint naming conventions — see\n'
            '  prisma/sa/_types.py for what that involves.'
        )


def check_provider(provider: str) -> None:
    if provider not in SUPPORTED_PROVIDERS:
        raise UnsupportedProviderError(provider)


# PostgreSQL, as created by `prisma db push`:
#
#   String    text                 Json     jsonb
#   Boolean   boolean              Bytes    bytea
#   Int       integer              Decimal  numeric(65,30)
#   BigInt    bigint               DateTime timestamp(3) without time zone
#   Float     double precision     String[] text[]  (and always NULLable)
_POSTGRESQL: Dict[str, Callable[[], Any]] = {
    'String': sa.Text,
    'Boolean': sa.Boolean,
    'Int': sa.Integer,
    'BigInt': sa.BigInteger,
    'Float': sa.Double,
    'Decimal': lambda: sa.Numeric(precision=65, scale=30),
    'DateTime': lambda: postgresql.TIMESTAMP(precision=3),
    'Json': postgresql.JSONB,
    'Bytes': postgresql.BYTEA,
}

# Recorded for whoever adds SQLite next, verified the same way. The scalar types
# are TEXT / BOOLEAN / INTEGER / BIGINT / REAL / DECIMAL / DATETIME / BLOB, and
# Prisma rejects `Json`, `enum` and scalar lists on SQLite outright, so those
# three branches are unreachable rather than wrong. The structural difference
# that stops SQLite being a one-line addition: Prisma emits uniques as `CREATE
# UNIQUE INDEX`, not as table constraints, so `UniqueConstraint` would diff.
_SCALARS: Dict[str, Dict[str, Callable[[], Any]]] = {
    'postgresql': _POSTGRESQL,
}


# `@db.*` overrides, as created by `prisma db push`. Verified:
#
#   @db.Uuid             -> uuid
#   @db.VarChar(40)      -> character varying(40)
#   @db.Text             -> text
#   @db.Decimal(12, 2)   -> numeric(12,2)
#   @db.Timestamptz(6)   -> timestamp(6) with time zone
#   @db.SmallInt         -> smallint
#
# Prisma does not send these on the wire at all; they are recovered by lexing the
# raw schema text (`generator/_native_types.py`). Without them a `@db.Uuid`
# primary key reads as `text`, and correcting that later is a table rewrite.
_PG_NATIVE: Dict[str, Callable[..., Any]] = {
    'Uuid': postgresql.UUID,
    'Text': sa.Text,
    'VarChar': lambda n=None: sa.String(int(n)) if n else sa.Text(),
    'Char': lambda n=None: sa.CHAR(int(n)) if n else sa.CHAR(),
    'Boolean': sa.Boolean,
    'Bit': lambda n=None: postgresql.BIT(int(n)) if n else postgresql.BIT(),
    'VarBit': lambda n=None: postgresql.BIT(int(n), varying=True) if n else postgresql.BIT(varying=True),
    'SmallInt': sa.SmallInteger,
    'Integer': sa.Integer,
    'BigInt': sa.BigInteger,
    'Oid': postgresql.OID,
    'Real': sa.REAL,
    'DoublePrecision': sa.Double,
    'Decimal': lambda p=None, s=None: sa.Numeric(int(p), int(s)) if p is not None else sa.Numeric(),
    'Money': postgresql.MONEY,
    'Date': sa.Date,
    'Time': lambda p=None: sa.Time(precision=int(p)) if p is not None else sa.Time(),
    'Timetz': lambda p=None: sa.Time(precision=int(p), timezone=True) if p is not None else sa.Time(timezone=True),
    'Timestamp': lambda p=None: postgresql.TIMESTAMP(precision=int(p) if p is not None else None),
    'Timestamptz': lambda p=None: postgresql.TIMESTAMP(precision=int(p) if p is not None else None, timezone=True),
    'ByteA': postgresql.BYTEA,
    'Json': postgresql.JSON,
    'JsonB': postgresql.JSONB,
    'Inet': postgresql.INET,
    'Xml': lambda: sa.types.NullType(),
    'Citext': lambda: sa.Text(),
}


def native_type(provider: str, name: str, args: List[str]) -> Any:
    """The column type for a `@db.*` annotation.

    Unknown annotations raise rather than falling back to the default scalar
    type: falling back is how a `@db.Uuid` becomes `text`, which is exactly the
    failure this table exists to prevent.
    """
    check_provider(provider)
    try:
        factory = _PG_NATIVE[name]
    except KeyError:
        raise NotImplementedError(
            f'No {provider} mapping for the native type annotation @db.{name}.\n'
            '  Falling back to the default type would silently change the column, so this refuses instead.\n'
            '  Add it to prisma/sa/_types.py, checked against a real `prisma db push`.'
        ) from None

    try:
        return factory(*args)
    except (TypeError, ValueError) as exc:
        raise NotImplementedError(f'Could not apply @db.{name}({", ".join(args)}): {exc}') from None


def scalar_type(provider: str, prisma_type: str, native: Optional[List[Any]] = None) -> Any:
    check_provider(provider)
    if native:
        return native_type(provider, native[0], list(native[1]))

    try:
        factory = _SCALARS[provider][prisma_type]
    except KeyError:
        raise NotImplementedError(f'No {provider} type mapping for Prisma type {prisma_type!r}') from None
    return factory()


def enum_type(provider: str, name: str, labels: List[str], metadata: sa.MetaData) -> Any:
    """The column type for an enum field.

    PostgreSQL gets a real enum type, created and dropped with the schema. The
    labels are the *database* labels — `@map` on an enum value means the Python
    name and the stored label differ.
    """
    check_provider(provider)
    return postgresql.ENUM(*labels, name=name, metadata=metadata, create_type=True)


def array_type(provider: str, inner: Any) -> Any:
    """A scalar list field, e.g. `tags String[]`."""
    check_provider(provider)
    if provider != 'postgresql':  # pragma: no cover — no other verified provider allows them
        raise NotImplementedError(
            f'Scalar list fields are not supported on {provider}; '
            'Prisma only allows them on PostgreSQL and CockroachDB'
        )
    return postgresql.ARRAY(inner)
