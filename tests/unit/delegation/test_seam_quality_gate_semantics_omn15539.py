# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Cross-boundary seam: Core and Market must agree on quality-gate semantics.

OMN-15539 / OMN-15464.

``ModelDelegationResult`` (omnibase_core, the canonical bus wire DTO) and
``ModelDelegateSkillResponse`` (omnimarket, the consumer-facing wire DTO) are two
ends of the same delegation terminal.  The Market response is built from the Core
result in ``handler_delegate_skill._response_from_result`` and is what the
``delegate-skill-completed.v1`` terminal event — and therefore the delegation
projection — is published from.

If Core rejects a contradictory quality verdict and Market accepts it, the
contradiction is not blocked, it is merely *relocated*: any producer that skips
the Core model (local dispatch port, a direct response construction, a future
adapter) publishes an unfalsifiable terminal.  That is the OMN-15464 failure mode
— a terminal that printed ``score_below_required_bar: actual_score=0.867
required_bar=0.800`` — with the label moved one hop downstream.

This test drives BOTH real models with the SAME fixture payload and asserts they
reach the same verdict.  It is deliberately not two independent unit suites: the
seam is the agreement, so the agreement is what is asserted.

Seam definition (field-by-field, Core name -> Market name):

    quality_passed              -> quality_gate_passed        bool
    quality_score               -> quality_score              float in [0.0, 1.0]
    required_quality_bar        -> required_quality_bar       float | None
    score_vs_required_bar       -> score_vs_required_bar      EnumQualityScoreComparison | None
    failed_acceptance_criteria  -> failed_acceptance_criteria tuple[str, ...]

Every other field on either model is outside this seam and is filled here with
the minimum valid, semantically neutral value so a failure can only come from the
seam itself.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from omnibase_core.models.delegation.wire import ModelDelegationResult
from pydantic import ValidationError

from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillResponse,
)

CONTRADICTION_DIR = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "seams"
    / "quality_gate"
    / "contradictions"
)

CONTRADICTION_CASES = ("a", "b", "c")


def _load_seam(case_id: str) -> dict[str, Any]:
    fixture_path = CONTRADICTION_DIR / f"{case_id}.json"
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    seam: dict[str, Any] = payload["seam"]
    return seam


def _core_kwargs(seam: dict[str, Any], correlation_id: Any) -> dict[str, Any]:
    """Map the seam onto Core's canonical result, neutral elsewhere."""
    return {
        "correlation_id": correlation_id,
        "task_type": "reasoning",
        "model_used": "seam-model",
        "endpoint_url": "https://seam.test/v1/chat/completions",
        "content": "seam content",
        "latency_ms": 0,
        "fallback_to_claude": False,
        "quality_passed": seam["quality_gate_passed"],
        "quality_score": seam["quality_score"],
        "required_quality_bar": seam["required_quality_bar"],
        "score_vs_required_bar": seam["score_vs_required_bar"],
        "failed_acceptance_criteria": tuple(seam["failed_acceptance_criteria"]),
    }


def _market_kwargs(seam: dict[str, Any], correlation_id: Any) -> dict[str, Any]:
    """Map the same seam onto Market's consumer-facing response."""
    return {
        "status": "completed" if seam["quality_gate_passed"] else "failed",
        "correlation_id": correlation_id,
        "task_type": "reasoning",
        "quality_gate_passed": seam["quality_gate_passed"],
        "quality_score": seam["quality_score"],
        "required_quality_bar": seam["required_quality_bar"],
        "score_vs_required_bar": seam["score_vs_required_bar"],
        "failed_acceptance_criteria": tuple(seam["failed_acceptance_criteria"]),
    }


@pytest.mark.unit
@pytest.mark.parametrize("case_id", CONTRADICTION_CASES)
def test_seam_quality_gate_semantics_agree_core_and_market(case_id: str) -> None:
    """Both wire models must reject the same contradictory quality verdict."""
    seam = _load_seam(case_id)
    correlation_id = uuid4()

    core_rejected = True
    try:
        ModelDelegationResult(**_core_kwargs(seam, correlation_id))
    except ValidationError:
        pass
    else:
        core_rejected = False

    market_rejected = True
    try:
        ModelDelegateSkillResponse(**_market_kwargs(seam, correlation_id))
    except ValidationError:
        pass
    else:
        market_rejected = False

    assert core_rejected, (
        f"seam case {case_id}: omnibase_core ModelDelegationResult ACCEPTED a "
        "contradictory quality verdict; the canonical bus invariant regressed"
    )
    assert market_rejected, (
        f"seam case {case_id}: omnimarket ModelDelegateSkillResponse ACCEPTED a "
        "quality verdict that omnibase_core rejects. The two ends of the "
        "delegation terminal disagree, so the contradiction is published rather "
        "than blocked (OMN-15464 class)."
    )


@pytest.mark.unit
@pytest.mark.parametrize("case_id", CONTRADICTION_CASES)
def test_seam_fixture_declares_every_seam_field(case_id: str) -> None:
    """Guard the fixture shape so a dropped key cannot vacuously pass above."""
    seam = _load_seam(case_id)
    assert set(seam) == {
        "quality_gate_passed",
        "quality_score",
        "required_quality_bar",
        "score_vs_required_bar",
        "failed_acceptance_criteria",
    }


@pytest.mark.unit
def test_seam_consistent_verdicts_are_accepted_by_both_ends() -> None:
    """Negative control: an internally consistent verdict passes both models."""
    correlation_id = uuid4()
    seam: dict[str, Any] = {
        "quality_gate_passed": False,
        "quality_score": 0.867,
        "required_quality_bar": 0.8,
        "score_vs_required_bar": "at_or_above_bar",
        "failed_acceptance_criteria": [
            "TASK_MISMATCH: failed step_by_step_explanation"
        ],
    }

    ModelDelegationResult(**_core_kwargs(seam, correlation_id))
    ModelDelegateSkillResponse(**_market_kwargs(seam, correlation_id))
