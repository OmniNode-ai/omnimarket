# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-13366: deterministic acceptance evidence for verifiable task classes."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta as quality_gate_delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_input import (
    ModelQualityGateInput,
)

_TASK_CLASS_CONTRACT = (
    Path(__file__).parents[3] / "src/omnimarket/configs/task_class_contracts.v1.yaml"
)


def _gate_input(
    *,
    task_type: str = "test",
    content: str,
    deterministic: tuple[str, ...],
    heuristic: tuple[str, ...] = (),
) -> ModelQualityGateInput:
    return ModelQualityGateInput(
        correlation_id=uuid4(),
        task_type=task_type,
        llm_response_content=content,
        dod_deterministic=deterministic,
        dod_heuristic=heuristic,
    )


@pytest.mark.unit
def test_verifiable_pass_emits_deterministic_acceptance_evidence() -> None:
    result = quality_gate_delta(
        _gate_input(
            content=(
                "```python\n"
                "import pytest\n\n"
                "@pytest.mark.unit\n"
                "def test_handles_empty_value():\n"
                "    assert normalize('') == ''\n"
                "```\n"
            ),
            deterministic=(
                "compiles_without_errors",
                "final_artifact_only",
                "uses_pytest_mark_unit",
            ),
        )
    )

    dumped = result.model_dump(by_alias=True)
    assert result.passed is True
    assert result.score_source == "deterministic_acceptance"
    assert result.acceptance_version == "delegation-deterministic-acceptance.v1"
    assert len(result.corpus_hash) == 64
    assert len(result.validator_or_artifact_hash) == 64
    assert "deterministic_acceptance" in result.acceptance_command
    assert result.actual_score == pytest.approx(1.0)
    assert dumped["pass"] is True
    assert result.failure_cases == ()


@pytest.mark.unit
def test_verifiable_fail_records_failure_cases_and_actual_score() -> None:
    result = quality_gate_delta(
        _gate_input(
            content="def test_missing_marker():\n    assert True\n",
            deterministic=(
                "compiles_without_errors",
                "final_artifact_only",
                "uses_pytest_mark_unit",
            ),
        )
    )

    dumped = result.model_dump(by_alias=True)
    assert result.passed is False
    assert result.fail_category == "fail_deterministic"
    assert result.actual_score == pytest.approx(0.667)
    assert dumped["pass"] is False
    assert result.failure_cases == ("TASK_MISMATCH: missing @pytest.mark.unit",)
    assert result.failure_reasons == result.failure_cases


@pytest.mark.unit
def test_verifiable_heuristic_markers_do_not_override_deterministic_authority() -> None:
    result = quality_gate_delta(
        _gate_input(
            task_type="code_generation",
            content="def normalize(value):\n    return value.strip()\n",
            deterministic=("compiles_without_errors", "passes_existing_tests"),
            heuristic=("covers_edge_cases",),
        )
    )

    assert result.passed is True
    assert result.fail_category == "pass"
    assert result.actual_score == pytest.approx(1.0)
    assert result.failure_reasons == ()
    assert result.failure_cases == ()


@pytest.mark.unit
def test_non_verifiable_contract_path_keeps_heuristic_authority() -> None:
    result = quality_gate_delta(
        _gate_input(
            task_type="research",
            content="This is probably fine.",
            deterministic=("response_non_empty",),
            heuristic=("cites_sources",),
        )
    )

    assert result.passed is False
    assert result.fail_category == "fail_heuristic"
    assert result.acceptance_version == ""
    assert result.actual_score is None
    assert any("source citations" in reason for reason in result.failure_reasons)


@pytest.mark.unit
@pytest.mark.parametrize(
    "task_class", ["code_generation", "test", "validator_generation"]
)
def test_task_contract_declares_deterministic_acceptance_authority(
    task_class: str,
) -> None:
    contract = yaml.safe_load(_TASK_CLASS_CONTRACT.read_text(encoding="utf-8"))
    entry = contract["task_classes"][task_class]

    assert entry["score_source"] == "deterministic_acceptance"
    authority = entry["acceptance_authority"]
    assert authority["type"] == "deterministic_acceptance"
    assert set(authority["evidence_fields"]) == {
        "acceptance_version",
        "corpus_hash",
        "validator_or_artifact_hash",
        "acceptance_command",
        "actual_score",
        "pass",
        "failure_cases",
    }
