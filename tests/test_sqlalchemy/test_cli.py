"""`prisma py sqlalchemy generate` / `baseline`.

`doctor` is covered by `test_doctor.py`; these are the two commands that write
files, so what matters here is where the bytes land and what is said about the
shapes that were left out.
"""

from __future__ import annotations

from typing import Any
from pathlib import Path

import pytest
from click.testing import CliRunner

from prisma.cli.commands.sqlalchemy import cli

pytest.importorskip('sqlalchemy', reason='prisma[sqlalchemy] is not installed')


@pytest.fixture(name='runner')
def runner_fixture() -> CliRunner:
    # separated so the emitted source can be read off stdout without the
    # refusal notes, which go to stderr precisely so it can be piped
    return CliRunner(mix_stderr=False)


def test_generate_writes_models_to_stdout(runner: CliRunner, installed: Any) -> None:
    result = runner.invoke(cli, ['generate'])

    assert result.exit_code == 0, result.output
    assert 'class Account(Base):' in result.stdout
    assert 'mapped_column' in result.stdout


def test_generate_reports_refusals_on_stderr(runner: CliRunner, installed: Any) -> None:
    """The reference schema carries a self-referential implicit m2m, which
    cannot be expressed without join conditions the DMMF does not carry.

    The emitted source also leaves a comment where the attribute would have
    been, so the absence is visible in the file itself — the point of putting
    the notes on stderr is that stdout stays pipeable, not that the refusal is
    mentioned only once.
    """
    result = runner.invoke(cli, ['generate'])

    assert result.exit_code == 0
    notes = [line for line in result.stderr.splitlines() if line.startswith('not emitted:')]
    assert notes, result.stderr
    compile(result.stdout, 'models.py', 'exec')


def test_generate_writes_a_file(runner: CliRunner, installed: Any, tmp_path: Path) -> None:
    out = tmp_path / 'nested' / 'models.py'

    result = runner.invoke(cli, ['generate', '--out', str(out)])

    assert result.exit_code == 0, result.output
    assert out.read_text().startswith('"""')
    assert f'wrote {out}' in result.stdout


def test_generate_strict_refuses_rather_than_emitting(runner: CliRunner, installed: Any, tmp_path: Path) -> None:
    out = tmp_path / 'models.py'

    result = runner.invoke(cli, ['generate', '--strict', '--out', str(out)])

    assert result.exit_code != 0
    assert not out.exists(), 'a strict run left a partial file behind'


def test_baseline_writes_an_alembic_project(runner: CliRunner, installed: Any, tmp_path: Path) -> None:
    out = tmp_path / 'migrations'

    result = runner.invoke(cli, ['baseline', '--out', str(out)])

    assert result.exit_code == 0, result.output
    assert (tmp_path / 'alembic.ini').exists()
    assert (out / 'env.py').exists()

    versions = list((out / 'versions').glob('*.py'))
    assert len(versions) == 1, 'a baseline is one revision, not a chain'
    assert 'baseline revision' in result.stdout


def test_baseline_points_env_at_generated_models_when_asked(runner: CliRunner, installed: Any, tmp_path: Path) -> None:
    out = tmp_path / 'migrations'

    result = runner.invoke(cli, ['baseline', '--out', str(out), '--models-import', 'myapp.models'])

    assert result.exit_code == 0, result.output
    env = (out / 'env.py').read_text()
    assert 'from myapp.models import Base' in env
    assert 'sa.metadata()' not in env


def test_baseline_defaults_to_prisma_sa_metadata(runner: CliRunner, installed: Any, tmp_path: Path) -> None:
    out = tmp_path / 'migrations'

    result = runner.invoke(cli, ['baseline', '--out', str(out)])

    assert result.exit_code == 0, result.output
    env = (out / 'env.py').read_text()
    assert 'from prisma import sa' in env
    assert 'sa.metadata()' in env
