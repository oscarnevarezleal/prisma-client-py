from types import SimpleNamespace
from typing import Any
from pathlib import Path

import pytest
from pydantic import ValidationError

from prisma._compat import (
    PYDANTIC_V2,
    model_json,
    model_parse,
    model_parse_json,
)
from prisma.generator.models import Config, Module, max_relation_chain_depth


def test_module_serialization() -> None:
    """Python module serialization to json"""
    path = Path(__file__).parent.parent.joinpath('scripts/partial_type_generator.py')
    module = model_parse(Module, {'spec': str(path)})
    assert model_parse_json(Module, model_json(module)).spec.name == module.spec.name


def test_recursive_type_depth() -> None:
    """Recursive type depth option disallows values that are less than 2 and do not equal -1."""
    for value in [-2, -3, 0, 1]:
        with pytest.raises(ValidationError) as exc:
            Config(recursive_type_depth=value)

        assert exc.match('Value must equal -1 or be greater than 1.')

    with pytest.raises(ValidationError) as exc:
        Config(
            recursive_type_depth='a'  # pyright: ignore[reportArgumentType]
        )

    if PYDANTIC_V2:
        assert exc.match('Input should be a valid integer, unable to parse string as an integer')
    else:
        assert exc.match('value is not a valid integer')

    for value in [-1, 2, 3, 10, 99]:
        config = Config(recursive_type_depth=value)
        assert config.recursive_type_depth == value


def test_default_recursive_type_depth(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Warn when recursive type depth is not set:

    https://github.com/RobertCraigie/prisma-client-py/issues/252

    Ensure that we provide advice on what value to use and that it defaults to 5.

    Also validate that when a type depth is provided, no warning is shown.
    """
    c = Config()
    captured = capsys.readouterr()
    assert 'it is highly recommended to use Pyright' in captured.out.replace('\n', ' ')
    assert c.recursive_type_depth == 5

    c = Config(recursive_type_depth=5)
    captured = capsys.readouterr()
    assert 'it is highly recommended to use Pyright' not in captured.out.replace('\n', ' ')
    assert c.recursive_type_depth == 5

    c = Config(recursive_type_depth=2)
    captured = capsys.readouterr()
    assert 'it is highly recommended to use Pyright' not in captured.out.replace('\n', ' ')
    assert c.recursive_type_depth == 2


def test_recursive_validation_models_option() -> None:
    """The recursiveValidationModels option parses (default off, snake_case, camelCase)."""
    assert Config().recursive_validation_models is False
    assert Config(recursive_validation_models=True).recursive_validation_models is True
    # the schema passes the camelCase alias
    assert (
        Config(recursiveValidationModels=True).recursive_validation_models  # pyright: ignore[reportCallIssue]
        is True
    )


def _model(name: str, *relation_targets: str) -> Any:
    # lightweight stand-in for generator.models.Model (only the attributes
    # max_relation_chain_depth reads); typed as Any so it satisfies List[Model].
    fields = [SimpleNamespace(relation_name='r', type=target) for target in relation_targets]
    return SimpleNamespace(name=name, all_fields=fields)


def test_max_relation_chain_depth() -> None:
    """Longest simple path through the relation graph (the recursion the runtime sizes for)."""
    # no models / single model
    assert max_relation_chain_depth([]) == 0
    assert max_relation_chain_depth([_model('A')]) == 1

    # no relations between two models
    assert max_relation_chain_depth([_model('A'), _model('B')]) == 1

    # linear chain (with back-edges, like the generated `next`/`prev`) -> N
    chain = []
    for i in range(6):
        targets = []
        if i < 5:
            targets.append(f'M{i + 1}')
        if i > 0:
            targets.append(f'M{i - 1}')
        chain.append(_model(f'M{i}', *targets))
    assert max_relation_chain_depth(chain) == 6

    # star/hub: longest simple path is spoke -> hub -> spoke == 3
    star = [_model('Hub', *[f'S{i}' for i in range(20)])] + [_model(f'S{i}', 'Hub') for i in range(20)]
    assert max_relation_chain_depth(star) == 3

    # the result never exceeds the model count
    assert max_relation_chain_depth(chain) <= len(chain)
