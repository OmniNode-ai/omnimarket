# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for cli_ab_compare — arg parsing and table rendering.

Uses click.testing.CliRunner + a mock orchestrator. No network, no LLM calls.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from omnimarket.cli.cli_ab_compare import _render_table_plain, main
from omnimarket.nodes.node_ab_compare_orchestrator.models.model_ab_compare_result import (
    ModelAbCompareResult,
    ModelComparisonRow,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_ROW_LOCAL = ModelComparisonRow(
    model_key="qwen3-coder-30b",
    display_name="Qwen3-Coder-30B",
    prompt_tokens=100,
    completion_tokens=200,
    total_tokens=300,
    cost_usd=0.0,
    latency_ms=4200,
    quality="pass",
)

_ROW_CLOUD = ModelComparisonRow(
    model_key="claude-sonnet",
    display_name="Claude Sonnet",
    prompt_tokens=120,
    completion_tokens=250,
    total_tokens=370,
    cost_usd=0.04125,
    latency_ms=3800,
    quality="pass",
)

_RESULT_TWO_MODELS = ModelAbCompareResult(
    comparison=[_ROW_LOCAL, _ROW_CLOUD],
    correlation_id="test-corr-123",
    status="COMPLETED",
    models_skipped=[],
)

_RESULT_PARTIAL = ModelAbCompareResult(
    comparison=[_ROW_LOCAL],
    correlation_id="test-corr-456",
    status="PARTIAL",
    models_skipped=["deepseek-r1-14b"],
)


def _make_mock_handler(result: ModelAbCompareResult) -> MagicMock:
    handler = MagicMock()
    handler.handle = AsyncMock(return_value=result)
    return handler


# ---------------------------------------------------------------------------
# Arg parsing tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_help_prints_usage() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "--task" in result.output
    assert "--models" in result.output
    assert "--output" in result.output


@pytest.mark.unit
def test_cli_requires_task_arg() -> None:
    runner = CliRunner()
    result = runner.invoke(main, [])
    assert result.exit_code != 0
    assert "Missing option" in result.output or "Error" in result.output


@pytest.mark.unit
def test_cli_default_models_is_all() -> None:
    runner = CliRunner()
    captured_command: list = []

    def fake_run(
        task: str,
        models: list[str],
        system_prompt: object,
        quality_check: bool,
        output: str,
    ) -> int:
        captured_command.append(models)
        return 0

    with patch("omnimarket.cli.cli_ab_compare._run", side_effect=fake_run):
        result = runner.invoke(main, ["--task", "test task"])

    assert result.exit_code == 0
    assert captured_command[0] == ["all"]


@pytest.mark.unit
def test_cli_parses_comma_separated_models() -> None:
    runner = CliRunner()
    captured_command: list = []

    def fake_run(
        task: str,
        models: list[str],
        system_prompt: object,
        quality_check: bool,
        output: str,
    ) -> int:
        captured_command.append(models)
        return 0

    with patch("omnimarket.cli.cli_ab_compare._run", side_effect=fake_run):
        result = runner.invoke(
            main, ["--task", "test", "--models", "qwen3-coder-30b,claude-sonnet"]
        )

    assert result.exit_code == 0
    assert captured_command[0] == ["qwen3-coder-30b", "claude-sonnet"]


@pytest.mark.unit
def test_cli_output_json_flag() -> None:
    runner = CliRunner()
    captured_output: list = []

    def fake_run(
        task: str,
        models: list[str],
        system_prompt: object,
        quality_check: bool,
        output: str,
    ) -> int:
        captured_output.append(output)
        return 0

    with patch("omnimarket.cli.cli_ab_compare._run", side_effect=fake_run):
        runner.invoke(main, ["--task", "test", "--output", "json"])

    assert captured_output[0] == "json"


@pytest.mark.unit
def test_cli_quality_check_flag() -> None:
    runner = CliRunner()
    captured: list = []

    def fake_run(
        task: str,
        models: list[str],
        system_prompt: object,
        quality_check: bool,
        output: str,
    ) -> int:
        captured.append(quality_check)
        return 0

    with patch("omnimarket.cli.cli_ab_compare._run", side_effect=fake_run):
        runner.invoke(main, ["--task", "test", "--quality-check"])

    assert captured[0] is True


@pytest.mark.unit
def test_cli_system_prompt_passthrough() -> None:
    runner = CliRunner()
    captured: list = []

    def fake_run(
        task: str,
        models: list[str],
        system_prompt: object,
        quality_check: bool,
        output: str,
    ) -> int:
        captured.append(system_prompt)
        return 0

    with patch("omnimarket.cli.cli_ab_compare._run", side_effect=fake_run):
        runner.invoke(main, ["--task", "test", "--system-prompt", "Be concise."])

    assert captured[0] == "Be concise."


# ---------------------------------------------------------------------------
# Table rendering tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_render_table_plain_contains_model_names(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _render_table_plain(_RESULT_TWO_MODELS)
    captured = capsys.readouterr()
    assert "Qwen3-Coder-30B" in captured.out
    assert "Claude Sonnet" in captured.out


@pytest.mark.unit
def test_render_table_plain_shows_zero_cost_for_local(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _render_table_plain(_RESULT_TWO_MODELS)
    captured = capsys.readouterr()
    assert "$0.0000" in captured.out


@pytest.mark.unit
def test_render_table_plain_shows_cloud_cost(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _render_table_plain(_RESULT_TWO_MODELS)
    captured = capsys.readouterr()
    assert "$0.0413" in captured.out or "$0.04125" in captured.out


@pytest.mark.unit
def test_render_table_plain_shows_savings(capsys: pytest.CaptureFixture[str]) -> None:
    _render_table_plain(_RESULT_TWO_MODELS)
    captured = capsys.readouterr()
    assert "SAVINGS" in captured.out


@pytest.mark.unit
def test_render_table_plain_shows_skipped_on_partial(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _render_table_plain(_RESULT_PARTIAL)
    captured = capsys.readouterr()
    assert "deepseek-r1-14b" in captured.out


@pytest.mark.unit
def test_render_table_plain_quality_column(capsys: pytest.CaptureFixture[str]) -> None:
    _render_table_plain(_RESULT_TWO_MODELS)
    captured = capsys.readouterr()
    assert "pass" in captured.out


# ---------------------------------------------------------------------------
# JSON output test
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_render_json_is_valid(capsys: pytest.CaptureFixture[str]) -> None:
    from omnimarket.cli.cli_ab_compare import _render_json

    _render_json(_RESULT_TWO_MODELS)
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["status"] == "COMPLETED"
    assert len(data["comparison"]) == 2
    assert data["comparison"][0]["model_key"] == "qwen3-coder-30b"


@pytest.mark.unit
def test_render_json_includes_all_fields(capsys: pytest.CaptureFixture[str]) -> None:
    from omnimarket.cli.cli_ab_compare import _render_json

    _render_json(_RESULT_TWO_MODELS)
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    row = data["comparison"][0]
    assert "prompt_tokens" in row
    assert "completion_tokens" in row
    assert "cost_usd" in row
    assert "latency_ms" in row


# ---------------------------------------------------------------------------
# Integration: _run calls handler and returns correct exit code
# ---------------------------------------------------------------------------


_HANDLER_MODULE = (
    "omnimarket.nodes.node_ab_compare_orchestrator"
    ".handlers.handler_ab_compare_orchestrator.HandlerAbCompareOrchestrator"
)


@pytest.mark.unit
def test_run_completed_exits_zero() -> None:
    from omnimarket.cli.cli_ab_compare import _run

    mock_handler = _make_mock_handler(_RESULT_TWO_MODELS)

    with (
        patch(_HANDLER_MODULE, return_value=mock_handler),
        patch("omnimarket.cli.cli_ab_compare._render_table_plain"),
    ):
        code = _run("test task", ["all"], None, False, "table")

    assert code == 0


@pytest.mark.unit
def test_run_uses_uuid_correlation_id() -> None:
    from omnimarket.cli.cli_ab_compare import _run

    captured_commands: list = []
    mock_handler = MagicMock()

    async def fake_handle(cmd: object) -> ModelAbCompareResult:
        captured_commands.append(cmd)
        return _RESULT_TWO_MODELS

    mock_handler.handle = fake_handle

    with (
        patch(_HANDLER_MODULE, return_value=mock_handler),
        patch("omnimarket.cli.cli_ab_compare._render_table_plain"),
    ):
        _run("test task", ["all"], None, False, "table")

    assert len(captured_commands) == 1
    cmd = captured_commands[0]
    import uuid as uuid_mod

    uuid_mod.UUID(cmd.correlation_id, version=4)
