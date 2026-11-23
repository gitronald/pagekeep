"""Tests for pagekeep."""

from typer.testing import CliRunner

from pagekeep.cli import app

runner = CliRunner()


def test_hello() -> None:
    result = runner.invoke(app)
    assert result.exit_code == 0
    assert "Hello from pagekeep!" in result.output
