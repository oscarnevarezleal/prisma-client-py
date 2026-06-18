"""Tests for the ``separate_model_files`` generator option.

The option is off by default, so the default generated client does not contain
a ``models/`` package. Rather than depend on a separately generated client,
these tests exercise the option's parsing and the templates that back it.
"""

from pathlib import Path

from prisma.generator.models import Config

TEMPLATES_DIR = Path(__file__).parent.parent / 'src' / 'prisma' / 'generator' / 'templates' / 'models'


def test_separate_model_files_option() -> None:
    """The separateModelFiles option parses and defaults to off."""
    assert Config().separate_model_files is False
    assert Config(separate_model_files=True).separate_model_files is True


def test_separate_model_templates_exist() -> None:
    """The per-model templates that back the option are present."""
    assert (TEMPLATES_DIR / '__init__.py.jinja').is_file()
    assert (TEMPLATES_DIR / '_model.py.jinja').is_file()


def test_models_package_template_wires_lazy_loading() -> None:
    """The generated models package uses lazy ``__getattr__`` loading + a cache."""
    content = (TEMPLATES_DIR / '__init__.py.jinja').read_text()
    for marker in ('__getattr__', '_model_cache', '_rebuild_models', '_rebuilding', '__all__'):
        assert marker in content, f'{marker} missing from models/__init__.py.jinja'


def test_model_template_defines_partial_helpers() -> None:
    """Each per-model file exposes ``create_partial`` and field metadata."""
    content = (TEMPLATES_DIR / '_model.py.jinja').read_text()
    assert 'create_partial' in content
    assert 'PartialModelField' in content
