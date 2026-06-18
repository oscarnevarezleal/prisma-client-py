"""Runtime support for the ``recursive_validation_models`` generator option.

When that option is enabled, generated models use ``defer_build=True`` and are
compiled lazily on first use. Compiling a chain of related models recurses ~14
Python stack frames per relation level, so a deep schema can exceed the default
recursion limit (and, if the limit alone were raised, overflow the C stack and
segfault).

``ensure_built`` performs that first compile inside a worker thread with an
*enlarged stack* and a recursion limit sized to the schema's longest relation
chain, so:

* the build is done off the default (small) stack -> no segfault, an
  under-estimate surfaces as a catchable ``RecursionError``;
* the limit is raised lazily (on first use) rather than as an import side effect;
* it runs once per model and is cached by Pydantic (``__pydantic_complete__``).
"""

from __future__ import annotations

import sys
import threading
from typing import TYPE_CHECKING, Any, List, Type, Callable

if TYPE_CHECKING:
    from pydantic import BaseModel

# ~14 stack frames are consumed per relation level when Pydantic builds a model's
# core schema (measured); HEADROOM covers the non-recursive base of the build.
_FRAMES_PER_LEVEL = 14
_RECURSION_HEADROOM = 2000
# rough bytes of C stack per allowed recursion level, with a sane ceiling
_STACK_BYTES_PER_LEVEL = 4096
_MAX_STACK_BYTES = 512 * 1024 * 1024
_MB = 1024 * 1024

# serialise builds: `threading.stack_size` is process-global, so two concurrent
# builds must not race on it.
_build_lock = threading.Lock()


def required_recursion_limit(max_relation_depth: int) -> int:
    return _FRAMES_PER_LEVEL * max(1, max_relation_depth) + _RECURSION_HEADROOM


def _stack_bytes(limit: int) -> int:
    raw = min(max(8 * _MB, limit * _STACK_BYTES_PER_LEVEL), _MAX_STACK_BYTES)
    return ((raw + _MB - 1) // _MB) * _MB  # whole MB; satisfies stack_size constraints


def run_with_recursion_headroom(fn: Callable[[], Any], max_relation_depth: int) -> Any:
    """Run ``fn`` with recursion headroom for ``max_relation_depth`` levels, inside
    a worker thread given an enlarged stack. Exceptions (incl. ``RecursionError``)
    propagate to the caller. The raised recursion limit is left in place so deeper
    *data* validation keeps working; it is only ever raised, never lowered."""
    limit = required_recursion_limit(max_relation_depth)
    result: List[Any] = []
    error: List[BaseException] = []

    def target() -> None:
        try:
            result.append(fn())
        except BaseException as exc:  # noqa: BLE001 - re-raised on the calling thread
            error.append(exc)

    with _build_lock:
        if sys.getrecursionlimit() < limit:
            sys.setrecursionlimit(limit)
        previous_stack = None
        try:
            try:
                previous_stack = threading.stack_size(_stack_bytes(limit))
            except (ValueError, RuntimeError):
                previous_stack = None  # platform without thread-stack control
            thread = threading.Thread(target=target, name='prisma-model-build')
            thread.start()
            thread.join()
        finally:
            if previous_stack is not None:
                try:
                    threading.stack_size(previous_stack)
                except (ValueError, RuntimeError):
                    pass

    if error:
        raise error[0]
    return result[0] if result else None


def ensure_built(model: Type['BaseModel'], max_relation_depth: int) -> None:
    """Compile ``model``'s validator (and its relation closure) if not already
    done, safely, the first time the model is used."""
    if getattr(model, '__pydantic_complete__', True):
        return

    def _build() -> None:
        model.model_rebuild()

    try:
        run_with_recursion_headroom(_build, max_relation_depth)
    except RecursionError as exc:
        raise RecursionError(_limit_message(model, max_relation_depth)) from exc
    except Exception as exc:
        # Older pydantic-core (e.g. 2.8) surfaces a too-deep recursive build as its
        # own ``SchemaError`` ("recursion_loop") rather than a Python RecursionError;
        # normalise both to RecursionError so callers handle them uniformly.
        if type(exc).__name__ == 'SchemaError' and 'recursion' in str(exc).lower():
            raise RecursionError(_limit_message(model, max_relation_depth)) from exc
        raise


def _limit_message(model: Type['BaseModel'], max_relation_depth: int) -> str:
    return (
        f'Building the validator for {model.__name__!r} exceeded the recursion '
        f'limit while resolving related models (estimated relation depth '
        f'{max_relation_depth}). If your schema has a deeper relation chain, '
        f'raise the limit with `sys.setrecursionlimit(...)` before first use.'
    )
