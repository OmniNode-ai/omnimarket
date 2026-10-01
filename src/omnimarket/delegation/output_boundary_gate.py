# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Extraction refusals are deterministic floors on both delegation paths."""

from omnibase_core.models.delegation.wire import ModelDelegationOutputRefusal

from omnimarket.models.delegation.wire.model_quality_gate import ModelQualityGateResult


def gate_result_with_output_refusal(
    result: ModelQualityGateResult,
    refusal: ModelDelegationOutputRefusal | None,
    *,
    raw_content: str,
) -> ModelQualityGateResult:
    """Grade real content while keeping its unproven output boundary refused."""
    if refusal is None:
        return result
    reasons = tuple(
        reason
        for reason in result.failure_reasons
        if not raw_content.strip()
        or ("empty response" not in reason and "response is empty" not in reason)
    )
    return result.model_copy(
        update={
            "passed": False,
            "fail_category": "fail_deterministic",
            "failure_reasons": (
                f"DELIVERABLE_EXTRACTION: {refusal.reason.value}",
                *reasons,
            ),
            "fallback_recommended": True,
        }
    )
