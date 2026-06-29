# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-13373: ``no_refusal`` resolves in the deterministic acceptance path.

``no_refusal`` is a request-level ``SUPPORTED_ACCEPTANCE_CRITERIA`` value. The
orchestrator merges request ``acceptance_criteria`` into the deterministic DoD
band (see ``delta`` in handler_quality_gate). Before this fix the deterministic
evaluator had no dispatch branch for ``no_refusal`` and fell through to
``MALFORMED: unsupported deterministic DoD check 'no_refusal'`` — a non-clean,
non-escalation-worthy quality detail (MALFORMED is excluded from the fallback
verdict prefixes).

This module proves the reject-only pre-filter contract:

* a refusal in the deterministic path yields a ``REFUSAL:``-prefixed detail
  (escalation-worthy, NOT MALFORMED),
* a clean (non-refusal) output is still ``passed=False`` with the reject-only
  adequacy-authority reason — ``no_refusal`` alone never promotes output to
  adequate (OMN-13370 marker-authority retirement preserved).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from omnimarket.models.delegation.wire.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta as quality_gate_delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_input import (
    ModelQualityGateInput,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result import (
    ModelQualityGateResult,
)

_MALFORMED_NO_REFUSAL = "MALFORMED: unsupported deterministic DoD check 'no_refusal'"


def _gate_via_acceptance_criteria(content: str) -> ModelQualityGateResult:
    """Drive the gate the way a request does: no_refusal as acceptance_criteria.

    Building the request DTO first proves the criterion is accepted at the DTO
    boundary, and routing it through the deterministic band (acceptance_criteria
    extends dod_deterministic in ``delta``) reproduces the live forced-failure
    path that emitted the MALFORMED detail.
    """
    request = ModelDelegationRequest(
        prompt="summarize the change",
        task_type="summarization",
        correlation_id=uuid4(),
        emitted_at=datetime.now(UTC),
        acceptance_criteria=("no_refusal",),
    )
    return quality_gate_delta(
        ModelQualityGateInput(
            correlation_id=request.correlation_id,
            task_type=request.task_type,
            llm_response_content=content,
            dod_deterministic=request.acceptance_criteria,
        )
    )


@pytest.mark.unit
def test_no_refusal_acceptance_criterion_does_not_emit_malformed() -> None:
    """A clean output must not produce the MALFORMED unsupported-check detail."""
    result = _gate_via_acceptance_criteria(
        "The change adds a graded quality score for short delegated outputs."
    )

    assert all(reason != _MALFORMED_NO_REFUSAL for reason in result.failure_reasons), (
        result.failure_reasons
    )
    assert all(
        not reason.startswith("MALFORMED") for reason in result.failure_reasons
    ), result.failure_reasons


@pytest.mark.unit
def test_no_refusal_rejects_refusal_with_escalation_worthy_detail() -> None:
    """A refusal yields a REFUSAL: detail (escalation-worthy, not MALFORMED)."""
    result = _gate_via_acceptance_criteria("I cannot help with that request.")

    assert result.passed is False
    assert result.fail_category == "fail_deterministic"
    assert any(reason.startswith("REFUSAL") for reason in result.failure_reasons), (
        result.failure_reasons
    )
    assert all(
        not reason.startswith("MALFORMED") for reason in result.failure_reasons
    ), result.failure_reasons
    # REFUSAL is in the fallback verdict prefixes, so escalation is recommended.
    assert result.fallback_recommended is True


@pytest.mark.unit
def test_no_refusal_clean_output_cannot_promote_to_adequate() -> None:
    """Reject-only: a clean output is still not passed on no_refusal alone.

    Preserves OMN-13370 — schema/length/no-refusal/marker checks are reject-only
    and never grant adequacy authority by themselves.
    """
    result = _gate_via_acceptance_criteria(
        "The change adds a graded quality score for short delegated outputs."
    )

    assert result.passed is False
    assert result.fail_category == "fail_heuristic"
    assert result.failure_reasons == (
        "TASK_MISMATCH: no deterministic acceptance or judge adequacy authority; "
        "schema/length/no-refusal/marker checks are reject-only",
    )
    assert result.fallback_recommended is True
