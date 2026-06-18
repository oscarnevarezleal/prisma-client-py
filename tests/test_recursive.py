"""Tests for prisma._recursive, the runtime support for the
``recursiveValidationModels`` generator option (Pydantic v2 only)."""

import sys
import importlib
from pathlib import Path

import pytest

from prisma._compat import PYDANTIC_V2
from prisma._recursive import (
    ensure_built,
    required_recursion_limit,
    run_with_recursion_headroom,
)

pytestmark = pytest.mark.skipif(not PYDANTIC_V2, reason='recursiveValidationModels requires Pydantic v2')


@pytest.fixture(autouse=True)
def _restore_recursion_limit() -> object:
    # run_with_recursion_headroom only ever *raises* the global recursion limit;
    # restore it after each test so the limit doesn't leak across tests.
    old = sys.getrecursionlimit()
    yield
    sys.setrecursionlimit(old)


def _make_chain_module(tmp_path: Path, n: int, name: str) -> object:
    """Write and import a module of N chained, defer_build recursive models."""
    lines = [
        'from __future__ import annotations',
        'from typing import Optional',
        'from pydantic import BaseModel, ConfigDict',
        '',
    ]
    for i in range(n):
        lines.append(f'class M{i}(BaseModel):')
        lines.append('    model_config = ConfigDict(defer_build=True)')
        lines.append('    id: int')
        if i < n - 1:
            lines.append(f'    nxt: Optional[M{i + 1}] = None')
        lines.append('')
    (tmp_path / f'{name}.py').write_text('\n'.join(lines))

    sys.path.insert(0, str(tmp_path))
    try:
        sys.modules.pop(name, None)
        return importlib.import_module(name)
    finally:
        sys.path.remove(str(tmp_path))


def test_required_recursion_limit() -> None:
    # 14 frames per relation level + 2000 headroom, with a floor of one level
    assert required_recursion_limit(10) == 14 * 10 + 2000
    assert required_recursion_limit(200) == 14 * 200 + 2000
    assert required_recursion_limit(0) == 14 * 1 + 2000


def test_run_with_recursion_headroom_returns_value() -> None:
    assert run_with_recursion_headroom(lambda: 21 * 2, max_relation_depth=5) == 42


def test_run_with_recursion_headroom_propagates_exceptions() -> None:
    def boom() -> None:
        raise ValueError('kaboom')

    with pytest.raises(ValueError, match='kaboom'):
        run_with_recursion_headroom(boom, max_relation_depth=5)


def test_ensure_built_builds_and_validates(tmp_path: Path) -> None:
    mod = _make_chain_module(tmp_path, 5, 'rvm_basic')

    # deferred -> not built until we ask
    assert mod.M0.__pydantic_complete__ is False

    ensure_built(mod.M0, max_relation_depth=5)
    assert mod.M0.__pydantic_complete__ is True

    # full validation, including nested relations
    inst = mod.M0.model_validate({'id': 1, 'nxt': {'id': 2, 'nxt': {'id': 3}}})
    assert inst.id == 1
    assert inst.nxt.nxt.id == 3
    assert type(inst.nxt).__name__ == 'M1'

    # invalid nested data is rejected with a precise location
    with pytest.raises(Exception) as exc:
        mod.M0.model_validate({'id': 1, 'nxt': {'id': 'not-an-int'}})
    assert exc.type.__name__ == 'ValidationError'
    assert ('nxt', 'id') == exc.value.errors()[0]['loc']


def test_ensure_built_is_idempotent(tmp_path: Path) -> None:
    mod = _make_chain_module(tmp_path, 4, 'rvm_idem')
    ensure_built(mod.M0, max_relation_depth=4)
    # second call is a no-op (already complete) and must not raise
    ensure_built(mod.M0, max_relation_depth=4)
    assert mod.M0.__pydantic_complete__ is True


def test_ensure_built_under_provisioned_raises_catchably(tmp_path: Path) -> None:
    # A deep chain with a deliberately tiny depth estimate: the limit is lower than
    # the build actually needs, so it must raise a *catchable* RecursionError (run
    # in an enlarged-stack thread) rather than segfaulting the process.
    mod = _make_chain_module(tmp_path, 260, 'rvm_deep')

    with pytest.raises(RecursionError):
        ensure_built(mod.M0, max_relation_depth=1)

    # the process survived; a correctly-sized build then succeeds
    ensure_built(mod.M0, max_relation_depth=260)
    assert mod.M0.model_validate({'id': 1}).id == 1
