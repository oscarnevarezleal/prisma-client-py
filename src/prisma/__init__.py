# -*- coding: utf-8 -*-

__title__ = 'prisma'
__author__ = 'RobertCraigie'
__license__ = 'APACHE'
__copyright__ = 'Copyright 2020-2023 RobertCraigie'
__version__ = '0.15.0'

from typing import TYPE_CHECKING, Any

from . import errors as errors
from .utils import setup_logging
from ._types import PrismaMethod as PrismaMethod
from ._config import config as config
from ._metrics import (
    Metric as Metric,
    Metrics as Metrics,
    MetricHistogram as MetricHistogram,
)
from .validator import *

# Lazy loading cache to avoid re-importing
_lazy_imports: dict[str, Any] = {}

# Track if generated code exists
_generated_code_exists: bool | None = None


def _check_generated_code() -> bool:
    """Check if generated code exists."""
    global _generated_code_exists
    if _generated_code_exists is None:
        try:
            # Use importlib to check without triggering __getattr__
            import importlib.util
            import sys
            from pathlib import Path

            # Check if we're being called from the generator
            # If so, allow imports even if client.py doesn't exist yet
            import inspect
            frame = inspect.currentframe()
            while frame:
                frame_info = inspect.getframeinfo(frame)
                if 'generator' in frame_info.filename:
                    _generated_code_exists = True
                    return True
                frame = frame.f_back

            # Check if client.py exists
            module_path = Path(__file__).parent / 'client.py'
            _generated_code_exists = module_path.exists()
        except Exception:
            _generated_code_exists = False
    return _generated_code_exists


def _lazy_load_module(name: str) -> Any:
    """Lazy load a module and cache it."""
    if name in _lazy_imports:
        return _lazy_imports[name]

    # Allow imports of these modules even during generation
    # They are needed by the generated model files themselves
    allowed_during_generation = {'enums', 'fields', 'types', 'bases', 'errors'}
    
    # Check if generated code exists
    if not _check_generated_code():
        # Allow certain modules during generation
        if name not in allowed_during_generation:
            if name in {'Prisma', 'Client', 'models', 'types', 'bases', 'partials'}:
                raise RuntimeError(
                    "The Client hasn't been generated yet, "
                    'you must run `prisma generate` before you can use the client.\n'
                    'See https://prisma-client-py.readthedocs.io/en/stable/reference/troubleshooting/#client-has-not-been-generated-yet'
                )
            raise AttributeError(f"module '{__name__}' has no attribute '{name}'")

    # Use importlib to avoid triggering __getattr__ recursion
    import importlib
    import sys

    # Import the requested module/attribute
    try:
        # Handle enums and fields which may be needed during generation
        if name == 'enums':
            try:
                module = importlib.import_module('.enums', package=__name__)
                _lazy_imports['enums'] = module
                return module
            except (ImportError, ModuleNotFoundError):
                # If it doesn't exist yet, that's okay during generation
                raise AttributeError(f"module '{__name__}' has no attribute '{name}'")
        elif name == 'fields':
            try:
                module = importlib.import_module('.fields', package=__name__)
                _lazy_imports['fields'] = module
                return module
            except (ImportError, ModuleNotFoundError):
                # If it doesn't exist yet, that's okay during generation
                raise AttributeError(f"module '{__name__}' has no attribute '{name}'")
        elif name == 'types':
            try:
                module = importlib.import_module('.types', package=__name__)
                _lazy_imports['types'] = module
                return module
            except (ImportError, ModuleNotFoundError):
                # If it doesn't exist yet, that's okay during generation
                raise AttributeError(f"module '{__name__}' has no attribute '{name}'")
        elif name == 'bases':
            try:
                module = importlib.import_module('.bases', package=__name__)
                _lazy_imports['bases'] = module
                return module
            except (ImportError, ModuleNotFoundError):
                # If it doesn't exist yet, that's okay during generation
                raise AttributeError(f"module '{__name__}' has no attribute '{name}'")
        elif name == 'models':
            module = importlib.import_module('.models', package=__name__)
            _lazy_imports['models'] = module
            return module
        elif name == 'types':
            module = importlib.import_module('.types', package=__name__)
            _lazy_imports['types'] = module
            return module
        elif name == 'bases':
            module = importlib.import_module('.bases', package=__name__)
            _lazy_imports['bases'] = module
            return module
        elif name == 'partials':
            module = importlib.import_module('.partials', package=__name__)
            _lazy_imports['partials'] = module
            return module
        elif name in {'Prisma', 'Client'}:
            # Import client module but only extract what's needed
            if 'client' not in _lazy_imports:
                client = importlib.import_module('.client', package=__name__)
                _lazy_imports['client'] = client
                # Cache commonly used exports from client
                for attr in dir(client):
                    if not attr.startswith('_'):
                        _lazy_imports[attr] = getattr(client, attr)
            return _lazy_imports.get(name)
        else:
            # Try to get from client module
            if 'client' not in _lazy_imports:
                client = importlib.import_module('.client', package=__name__)
                _lazy_imports['client'] = client

            client_module = _lazy_imports['client']
            if hasattr(client_module, name):
                attr = getattr(client_module, name)
                _lazy_imports[name] = attr
                return attr

            # Try fields module
            try:
                fields = importlib.import_module('.fields', package=__name__)
                if hasattr(fields, name):
                    attr = getattr(fields, name)
                    _lazy_imports[name] = attr
                    return attr
            except (ImportError, ModuleNotFoundError):
                pass

            raise AttributeError(f"module '{__name__}' has no attribute '{name}'")
    except ImportError as e:
        raise AttributeError(f"module '{__name__}' has no attribute '{name}'") from e


# Only eagerly load essential items for TYPE_CHECKING
if TYPE_CHECKING:
    try:
        from .client import *  # noqa: I001, TID251, F403
        from .fields import *  # noqa: TID251, F403
        from . import (
            bases as bases,  # noqa: TID251
            types as types,  # noqa: TID251
            models as models,  # noqa: TID251
            partials as partials,  # noqa: TID251
        )
    except ModuleNotFoundError:
        pass


def __getattr__(name: str) -> Any:
    """Lazy load attributes on access."""
    return _lazy_load_module(name)


# For dir() and help() support
def __dir__() -> list[str]:
    """Return list of available attributes."""
    base_attrs = [
        '__title__', '__author__', '__license__', '__copyright__', '__version__',
        'errors', 'config', 'Metric', 'Metrics', 'MetricHistogram', 'PrismaMethod',
    ]

    if _check_generated_code():
        base_attrs.extend(['Prisma', 'Client', 'models', 'types', 'bases', 'partials'])

    return base_attrs


setup_logging()
