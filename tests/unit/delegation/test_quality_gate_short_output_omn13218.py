# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Short-output quality-gate behavior (OMN-13218).

The delegation quality gate previously hard-failed behaviorally-correct SHORT
outputs on a blunt 100-char length floor (`WEAK_OUTPUT: response length N below
minimum M`). For short-output task classes (summarization, classification,
extraction) the correct answer is legitimately short, so the floor rejected
correct tier-1 output and forced wasteful escalation to the ceiling tier (which
then dead-ended on `no_higher_tier_available`).

The fix replaces the absolute character floor with a `semantic_adequacy`
heuristic: a complete short answer (at least one terminated sentence or a
complete structured artifact) is adequate; an empty / whitespace-only /
mid-token-truncated / bare fragment is not.

These tests pin the behavioral acceptance criteria:
  * A correct short summarization output passes at tier 1 (no escalation).
  * A genuinely truncated / empty output still fails.
  * The blunt `min_length_chars_N` floor no longer hard-fails short content.
"""

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

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "configs"
    / "task_class_contracts.v1.yaml"
)

# A correct, complete one-sentence summary. ~95 chars — below the old 100-char
# floor that wrongly rejected it. Contains no refusal and no accuracy disclaimer.
_CORRECT_SHORT_SUMMARY = (
    "The change adds a graded quality score so the gate discriminates output."
)


def _task_class_dod(task_class: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    contract = yaml.safe_load(_CONTRACT_PATH.read_text())
    dod = contract["task_classes"][task_class]["definition_of_done"]
    return (
        tuple(dod.get("deterministic", ())),
        tuple(dod.get("heuristic", ())),
    )


def _score(task_class: str, content: str) -> ModelQualityGateInput:
    det, heur = _task_class_dod(task_class)
    return ModelQualityGateInput(
        correlation_id=uuid4(),
        task_type=task_class,
        llm_response_content=content,
        dod_deterministic=det,
        dod_heuristic=heur,
    )


@pytest.mark.unit
def test_correct_short_summarization_passes_at_tier_one() -> None:
    """A correct short summary must PASS — not escalate on a length floor."""
    result = quality_gate_delta(_score("summarization", _CORRECT_SHORT_SUMMARY))

    assert result.passed, (
        f"correct short summary failed the gate: {result.failure_reasons}"
    )
    assert not result.fallback_recommended, (
        "correct short summary should not recommend escalation to a higher tier"
    )
    assert result.quality_score == pytest.approx(1.0)
    assert not any("below minimum" in r for r in result.failure_reasons), (
        f"length floor still firing on short content: {result.failure_reasons}"
    )


@pytest.mark.unit
def test_empty_summarization_still_fails() -> None:
    """A genuinely empty output must still fail (deterministic non-empty check)."""
    result = quality_gate_delta(_score("summarization", "   "))

    assert not result.passed
    assert result.fail_category == "fail_deterministic"


@pytest.mark.unit
def test_truncated_summarization_still_fails() -> None:
    """A mid-token-truncated fragment must still fail semantic adequacy."""
    result = quality_gate_delta(
        _score("summarization", "The change adds a graded quality score so the")
    )

    assert not result.passed, "truncated stub must not pass the gate"
    assert any(
        "semantic_adequacy" in r or "WEAK_OUTPUT" in r for r in result.failure_reasons
    ), (
        f"truncated stub did not trip an adequacy/weak-output reason: {result.failure_reasons}"
    )


@pytest.mark.unit
def test_correct_short_document_passes() -> None:
    """A correct short prose document must PASS instead of failing min_length_chars_200."""
    short_doc = "This module computes order totals and returns a typed summary object."
    result = quality_gate_delta(_score("document", short_doc))

    assert result.passed, f"correct short document failed: {result.failure_reasons}"
    assert not any("below minimum" in r for r in result.failure_reasons)


@pytest.mark.unit
def test_short_output_classes_drop_absolute_char_floor() -> None:
    """No short-output task class may carry a blunt min_length_chars_N heuristic."""
    contract = yaml.safe_load(_CONTRACT_PATH.read_text())
    offenders: list[str] = []
    for name in ("summarization", "document", "documentation"):
        heur = contract["task_classes"][name]["definition_of_done"].get("heuristic", [])
        for check in heur:
            if isinstance(check, str) and check.startswith("min_length_chars_"):
                offenders.append(f"{name}:{check}")
    assert not offenders, (
        "short-output classes still carry an absolute char floor (OMN-13218): "
        + ", ".join(offenders)
    )
