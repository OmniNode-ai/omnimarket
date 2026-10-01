# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the multi-run AB compare demo CLI."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from omnimarket.cli.cli_ab_compare_suite import main
from omnimarket.nodes.node_ab_compare_orchestrator.models.model_ab_compare_result import (
    ModelAbCompareResult,
    ModelComparisonRow,
)

if TYPE_CHECKING:
    from omnimarket.nodes.node_ab_compare_orchestrator.handlers.handler_ab_compare_orchestrator import (
        _ResolvedModel,
    )


def _result(correlation_id: str, tokens: int) -> ModelAbCompareResult:
    return ModelAbCompareResult(
        comparison=[
            ModelComparisonRow(
                model_key="qwen3-next-80b",
                display_name="Qwen3-Next-80B",
                total_tokens=tokens,
                prompt_tokens=tokens // 2,
                completion_tokens=tokens - (tokens // 2),
                cost_usd=0.0,
                latency_ms=1200,
            ),
            ModelComparisonRow(
                model_key="qwen3-coder-30b",
                display_name="Qwen3-Coder-30B",
                error="connection failed",
            ),
        ],
        correlation_id=correlation_id,
        status="COMPLETED",
        models_skipped=[],
    )


def _mock_handler() -> MagicMock:
    handler = MagicMock()
    handler.handle = AsyncMock(
        side_effect=[
            _result("corr-1", 100),
            _result("corr-2", 140),
        ]
    )
    return handler


@pytest.mark.unit
def test_cli_help_prints_suite_options() -> None:
    result = CliRunner().invoke(main, ["--help"])

    assert result.exit_code == 0
    assert "--task" in result.output
    assert "--tasks-file" in result.output
    assert "--models" in result.output
    assert "--transport" in result.output
    # OMN-20173: the direct GLM baseline is retired.
    assert "--include-glm" not in result.output
    assert "--no-include-glm" not in result.output
    assert "--output-file" in result.output


@pytest.mark.unit
def test_cli_runs_multiple_tasks_and_prints_aggregate() -> None:
    with patch(
        "omnimarket.nodes.node_ab_compare_orchestrator.handlers.handler_ab_compare_orchestrator.HandlerAbCompareOrchestrator",
        return_value=_mock_handler(),
    ):
        result = CliRunner().invoke(
            main,
            [
                "--task",
                "task one",
                "--task",
                "task two",
                "--models",
                "all",
                "--transport",
                "orchestrator",
                # OMN-20173: GLM is no longer an A/B suite option.
            ],
        )

    assert result.exit_code == 0
    assert "MULTI-RUN AB MODEL COMPARISON" in result.output
    assert "RUN 1: task one" in result.output
    assert "RUN 2: task two" in result.output
    assert "AGGREGATE BY MODEL" in result.output
    assert "Qwen3-Next-80B" in result.output
    assert "Qwen3-Coder-30B" in result.output
    assert " 2/2" in result.output


@pytest.mark.unit
def test_cli_json_output_is_machine_readable() -> None:
    with patch(
        "omnimarket.nodes.node_ab_compare_orchestrator.handlers.handler_ab_compare_orchestrator.HandlerAbCompareOrchestrator",
        return_value=_mock_handler(),
    ):
        result = CliRunner().invoke(
            main,
            [
                "--task",
                "task one",
                "--task",
                "task two",
                "--transport",
                "orchestrator",
                "--output",
                "json",
                # OMN-20173: GLM is no longer an A/B suite option.
            ],
        )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["tasks"] == ["task one", "task two"]
    assert len(payload["results"]) == 2
    aggregate = {item["model_key"]: item for item in payload["aggregate"]}
    assert aggregate["qwen3-next-80b"]["successes"] == 2
    assert aggregate["qwen3-next-80b"]["total_tokens"] == 240
    assert aggregate["qwen3-coder-30b"]["errors"] == 2


@pytest.mark.unit
def test_cli_writes_output_file(tmp_path: Path) -> None:
    output_file = tmp_path / "suite.txt"

    with patch(
        "omnimarket.nodes.node_ab_compare_orchestrator.handlers.handler_ab_compare_orchestrator.HandlerAbCompareOrchestrator",
        return_value=_mock_handler(),
    ):
        result = CliRunner().invoke(
            main,
            [
                "--task",
                "task one",
                "--task",
                "task two",
                "--transport",
                "orchestrator",
                "--output-file",
                str(output_file),
                # OMN-20173: GLM is no longer an A/B suite option.
            ],
        )

    assert result.exit_code == 0
    assert result.output == ""
    text = output_file.read_text(encoding="utf-8")
    assert "MULTI-RUN AB MODEL COMPARISON" in text
    assert "AGGREGATE BY MODEL" in text


@pytest.mark.unit
def test_cli_loads_tasks_file(tmp_path: Path) -> None:
    tasks_file = tmp_path / "tasks.txt"
    tasks_file.write_text(
        "# ignored\nfirst from file\nsecond from file\n", encoding="utf-8"
    )

    with patch(
        "omnimarket.nodes.node_ab_compare_orchestrator.handlers.handler_ab_compare_orchestrator.HandlerAbCompareOrchestrator",
        return_value=_mock_handler(),
    ):
        result = CliRunner().invoke(
            main,
            [
                "--tasks-file",
                str(tasks_file),
                "--transport",
                "orchestrator",
                "--output",
                "json",
                # OMN-20173: GLM is no longer an A/B suite option.
            ],
        )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["tasks"] == ["first from file", "second from file"]


@pytest.mark.unit
def test_cli_ignores_glm_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # OMN-20173: retain aggregation coverage while proving no GLM HTTP call.
    monkeypatch.setenv("LLM_GLM_URL", "https://api.z.ai/api/coding/paas/v4")
    monkeypatch.setenv("LLM_GLM_MODEL_NAME", "glm-4.5")
    with (
        patch(
            "omnimarket.nodes.node_ab_compare_orchestrator.handlers.handler_ab_compare_orchestrator.HandlerAbCompareOrchestrator",
            return_value=_mock_handler(),
        ),
        patch("httpx.AsyncClient.post", new_callable=AsyncMock) as post,
    ):
        result = CliRunner().invoke(
            main,
            [
                "--task",
                "task one",
                "--task",
                "task two",
                "--transport",
                "orchestrator",
                "--output",
                "json",
            ],
        )
    assert result.exit_code == 0
    post.assert_not_called()
    payload = json.loads(result.output)
    aggregate = {item["model_key"]: item for item in payload["aggregate"]}
    assert aggregate["qwen3-next-80b"]["successes"] == 2
    assert "glm-4.5" not in aggregate
    assert all(
        "glm" not in skipped
        for row in payload["results"]
        for skipped in row["models_skipped"]
    )


@pytest.mark.unit
def test_cli_direct_transport_uses_registry_calls() -> None:
    async def fake_call_registry_model_direct(**kwargs: object) -> ModelComparisonRow:
        model = cast("_ResolvedModel", kwargs["model"])
        return ModelComparisonRow(
            model_key=model.model_id,
            display_name=model.display_name,
            total_tokens=42,
            latency_ms=900,
        )

    with (
        patch(
            "omnimarket.nodes.node_ab_compare_orchestrator.handlers.handler_ab_compare_orchestrator._load_registry",
            return_value=[
                {
                    "id": "qwen3-coder-30b",
                    "display_name": "Qwen3-Coder-30B",
                    "endpoint": "http://example.local:8000",
                    "protocol": "openai_compatible",
                    "model_id": "qwen-test",
                    "cost_per_1k_input": 0.0,
                    "cost_per_1k_output": 0.0,
                    "location": "local",
                    "context_window": 8192,
                }
            ],
        ),
        patch(
            "omnimarket.cli.cli_ab_compare_suite._call_registry_model_direct",
            side_effect=fake_call_registry_model_direct,
        ),
    ):
        result = CliRunner().invoke(
            main,
            [
                "--task",
                "task one",
                "--models",
                "all",
                # OMN-20173: GLM is no longer an A/B suite option.
                "--output",
                "json",
            ],
        )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["results"][0]["comparison"][0]["model_key"] == "qwen3-coder-30b"
    assert payload["aggregate"][0]["successes"] == 1
