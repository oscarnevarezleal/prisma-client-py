"""Tests for minimal runtime optimization"""
import pytest
from pathlib import Path


def test_types_py_has_minimal_runtime():
    """Verify types.py uses minimal runtime when minimalRuntime=true"""
    types_path = Path("src/prisma/types.py")
    assert types_path.exists(), "types.py should exist"

    content = types_path.read_text()
    assert "_TypedDictProxy" in content, "Should have _TypedDictProxy class"
    assert "Minimal runtime" in content, "Should have minimal runtime comment"


def test_types_pyi_stub_exists():
    """Verify stub file exists for type checking"""
    stub_path = Path("src/prisma/types.pyi")
    assert stub_path.exists(), "types.pyi stub file should exist"

    # Stub should be much larger than runtime
    stub_size = stub_path.stat().st_size
    runtime_size = Path("src/prisma/types.py").stat().st_size

    assert stub_size > runtime_size * 10, "Stub file should be significantly larger than runtime file"


def test_typed_dict_proxy_instantiation():
    """Test that _TypedDictProxy can be instantiated like TypedDict"""
    from prisma.types import Model0CreateInput, Model0WhereInput

    # Test constructor instantiation
    create_input = Model0CreateInput(name="test", count=5)
    assert isinstance(create_input, dict)
    assert create_input["name"] == "test"
    assert create_input["count"] == 5

    # Test **kwargs instantiation
    data = {"name": "test2", "count": 10}
    where = Model0WhereInput(**data)
    assert isinstance(where, dict)
    assert where == data


def test_all_type_patterns_available():
    """Verify all common type patterns are available"""
    from prisma import types

    # Basic CRUD types
    assert hasattr(types, "Model0CreateInput")
    assert hasattr(types, "Model0UpdateInput")
    assert hasattr(types, "Model0WhereInput")

    # Optional types
    assert hasattr(types, "Model0OptionalCreateInput")

    # Query helpers
    assert hasattr(types, "Model0Include")
    assert hasattr(types, "Model0Select")

    # Relation types
    assert hasattr(types, "Model0CreateWithoutRelationsInput")

    # All should be _TypedDictProxy class
    assert types.Model0CreateInput.__name__ == "_TypedDictProxy"
    assert types.Model0Include.__name__ == "_TypedDictProxy"


def test_actions_pyi_stub_exists():
    """Verify actions stub file exists"""
    actions_pyi = Path("src/prisma/actions.pyi")
    assert actions_pyi.exists(), "actions.pyi should exist"

    # actions.pyi should be larger (has docstrings)
    pyi_size = actions_pyi.stat().st_size
    py_size = Path("src/prisma/actions.py").stat().st_size

    # With docstring stripping, .py should be smaller
    assert py_size < pyi_size, "Runtime actions.py should be smaller than actions.pyi"


def test_models_have_runtime_definitions():
    """Models need full runtime definitions for Pydantic"""
    from prisma import models

    # Check that models are actual Pydantic classes, not proxies
    assert hasattr(models, "Model0")

    # Should be a real class with fields
    Model0 = getattr(models, "Model0")
    assert hasattr(Model0, "model_fields") or hasattr(Model0, "__fields__"), \
        "Model should have Pydantic fields"


def test_config_minimal_runtime_field():
    """Verify Config has minimal_runtime field"""
    from prisma.generator.models import Config

    # Check field exists
    config = Config()
    assert hasattr(config, "minimal_runtime")

    # Default should be True
    assert config.minimal_runtime is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
