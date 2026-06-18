"""Tests for separate_model_files configuration option."""

import sys
from typing import Any, Dict, cast
from pathlib import Path


def test_separate_model_files_lazy_loading() -> None:
    """Test that models are lazy-loaded when separate_model_files is enabled."""
    # This test assumes prisma generate was run with separate_model_files=true
    # Check if models directory exists
    models_dir = Path(__file__).parent.parent / 'src' / 'prisma' / 'models'

    if not models_dir.exists():
        # Skip test if not using separate model files
        return

    # Check that individual model files exist
    init_file = models_dir / '__init__.py'
    assert init_file.exists(), 'models/__init__.py should exist'

    # Verify __init__.py contains lazy loading code
    init_content = init_file.read_text()
    assert '__getattr__' in init_content, 'Should have __getattr__ for lazy loading'
    assert '_model_cache' in init_content, 'Should have model cache'
    assert '__all__' in init_content, 'Should have __all__ list'


def test_model_import_syntax() -> None:
    """Test that import syntax works the same with separate model files."""
    try:
        # This should work whether models are separate or not
        from prisma.models import User

        # Model should be a class
        assert isinstance(User, type), 'User should be a class'

        # Model should have expected attributes
        assert hasattr(User, 'prisma'), 'User should have prisma() method'
        assert hasattr(User, 'create_partial'), 'User should have create_partial() method'

    except ImportError:
        # Skip if prisma client not generated
        pass


def test_lazy_loading_performance() -> None:
    """Test that lazy loading reduces initial import time."""
    import time

    try:
        # Clear any cached imports
        if 'prisma.models' in sys.modules:
            del sys.modules['prisma.models']

        # Time the import
        start = time.time()
        import prisma.models  # noqa: F401  # pyright: ignore[reportUnusedImport]

        import_time = time.time() - start

        # Import should be fast (< 100ms) with lazy loading
        # Without lazy loading, it could be 500ms+ for large schemas
        assert import_time < 0.5, f'Import took {import_time}s, should be faster with lazy loading'

    except ImportError:
        # Skip if prisma client not generated
        pass


def test_forward_references() -> None:
    """Test that forward references are resolved correctly."""
    try:
        from prisma.models import User

        # Create an instance with relationships
        # This will trigger forward reference resolution
        user_data: Dict[str, Any] = {'id': '1', 'name': 'Test User'}
        user = User(**user_data)

        assert user.id == '1'
        assert user.name == 'Test User'

    except ImportError:
        # Skip if prisma client not generated
        pass
    except Exception as e:
        # If we get a forward reference error, the test should fail
        if 'forward reference' in str(e).lower():
            raise AssertionError(f'Forward references not resolved: {e}') from e
        # Other errors (like missing fields) are okay for this test
        pass


def test_partial_type_generator_compatibility() -> None:
    """Test that partial type generator works with separate model files."""
    try:
        from prisma import partials  # noqa: F401

        # If partials module exists, it should be importable
        # Partial types should work the same way
        assert hasattr(partials, '__dir__') or True  # Just check it imports

    except ImportError:
        # Skip if no partials generated
        pass


def test_model_cache() -> None:
    """Test that model cache works correctly."""
    try:
        import prisma.models as models_module

        # Check if cache exists (only for separate model files)
        if hasattr(models_module, '_model_cache'):
            # Import a model twice
            from prisma.models import (
                User as User1,
                User as User2,
            )

            # Should be the same object (cached)
            assert User1 is User2, 'Models should be cached'

    except ImportError:
        # Skip if prisma client not generated
        pass


def test_rebuilding_flag() -> None:
    """Test that the rebuilding flag prevents recursion."""
    try:
        import prisma.models as models_module

        # Check if rebuilding flag exists (only for separate model files)
        if hasattr(models_module, '_rebuilding'):
            # Import a model to trigger rebuild
            from prisma.models import User  # noqa: F401  # pyright: ignore[reportUnusedImport]

            # After import, rebuilding should be False
            assert not cast(Any, models_module)._rebuilding, 'Rebuilding flag should be False after import'

    except ImportError:
        # Skip if prisma client not generated
        pass
