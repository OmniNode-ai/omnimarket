# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the delegation cost savings demo CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from omnimarket.cli.cli_delegation_cost_demo import main


def _invoke_isolated(
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
) -> object:
    monkeypatch.delenv("OMNI_HOME", raising=False)
    monkeypatch.delenv("LLM_CODER_MODEL_NAME", raising=False)
    runner = CliRunner()
    with runner.isolated_filesystem():
        return runner.invoke(main, args)


@pytest.mark.unit
def test_cli_help_prints_demo_options() -> None:
    result = CliRunner().invoke(main, ["--help"])

    assert result.exit_code == 0
    assert "--profile" in result.output
    assert "--local-model-id" in result.output
    assert "--cloud-cost-usd" in result.output
    assert "--output" in result.output


@pytest.mark.unit
def test_cli_default_profile_prints_joined_projection_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _invoke_isolated(monkeypatch, ["--output", "table"])

    assert result.exit_code == 0
    assert "DELEGATION COST SAVINGS PROOF" in result.output
    assert "profile=local_201" in result.output
    assert "task=Route one ticket-classification task" in result.output
    assert "local-qwen" in result.output
    assert "qwen3-coder-30b" in result.output
    assert "glm-4.5" in result.output
    assert "$   0.000084" in result.output
    assert "delegation_events:1" in result.output
    assert "llm_cost_aggregates:1" in result.output
    assert "savings_estimates:1" in result.output
    assert "usage_source=measured" in result.output


@pytest.mark.unit
def test_cli_json_output_contains_projected_rows_and_join(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _invoke_isolated(monkeypatch, ["--output", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["profile"]["name"] == "local_201"
    assert "ticket-classification task" in payload["task_text"]
    assert payload["profile"]["local_model_id"] == "qwen3-coder-30b"
    assert payload["rows"]["delegation_events"]["delegated_to"] == "local-qwen"
    assert payload["rows"]["llm_cost_aggregates"]["model_name"] == "qwen3-coder-30b"
    assert payload["rows"]["savings_estimates"]["model_cloud_baseline"] == "glm-4.5"
    assert payload["joined"]["correlation_id"] == "demo-2026-05-03-cost-routing-001"
    assert payload["joined"]["tokens"] == 123
    assert payload["joined"]["local_cost_usd"] == "0.000000"
    assert payload["joined"]["cloud_cost_usd"] == "0.000084"
    assert payload["joined"]["savings_usd"] == "0.000084"
    assert payload["joined"]["tables"] == {
        "delegation_events": 1,
        "llm_cost_aggregates": 1,
        "savings_estimates": 1,
    }


@pytest.mark.unit
def test_cli_laptop_profile_accepts_runtime_model_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _invoke_isolated(
        monkeypatch,
        [
            "--profile",
            "laptop_standalone",
            "--local-model-id",
            "qwen2.5-coder-7b",
            "--cloud-cost-usd",
            "0.000050",
            "--output",
            "table",
        ],
    )

    assert result.exit_code == 0
    assert "profile=laptop_standalone" in result.output
    assert "laptop-local" in result.output
    assert "qwen2.5-coder-7b" in result.output
    assert "$   0.000050" in result.output


@pytest.mark.unit
def test_cli_laptop_profile_requires_configured_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _invoke_isolated(
        monkeypatch,
        ["--profile", "laptop_standalone", "--output", "table"],
    )

    assert result.exit_code != 0
    assert "__SET_ON_LAPTOP__" in result.output
    assert "LLM_CODER_MODEL_NAME" in result.output


@pytest.mark.unit
def test_cli_rejects_token_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _invoke_isolated(
        monkeypatch,
        ["--total-tokens", "123", "--prompt-tokens", "74", "--completion-tokens", "50"],
    )

    assert result.exit_code != 0
    assert "prompt_tokens + completion_tokens must equal total_tokens" in result.output


@pytest.mark.unit
def test_cli_rejects_negative_savings(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _invoke_isolated(
        monkeypatch,
        ["--local-cost-usd", "0.000100", "--cloud-cost-usd", "0.000084"],
    )

    assert result.exit_code != 0
    assert "cloud_cost_usd must be >= local_cost_usd" in result.output


@pytest.mark.unit
def test_cli_loads_explicit_profile_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("OMNI_HOME", raising=False)
    profile_file = tmp_path / "profiles.yaml"
    profile_file.write_text(
        """
profiles:
  tiny_local:
    local:
      delegate_label: tiny-delegate
      model_id: phi4-mini
      source_label: local/test
      marginal_cost_usd: "0.000000"
      usage_source: MEASURED
    cloud_baseline:
      model_id: glm-4.5
      source_label: cloud/z.ai
""",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        main,
        [
            "--profiles-file",
            str(profile_file),
            "--profile",
            "tiny_local",
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["profile"]["local_delegate"] == "tiny-delegate"
    assert payload["profile"]["local_model_id"] == "phi4-mini"
    assert payload["joined"]["usage_source"] == "measured"
