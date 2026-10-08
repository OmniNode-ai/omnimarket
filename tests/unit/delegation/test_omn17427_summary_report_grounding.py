# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Regressions from summary run d5576698-a7ab-4448-a849-b7cd4b00ee23."""

from uuid import uuid4

import pytest
from omnibase_infra.utils.util_error_sanitization import sanitize_error_string

from omnimarket.delegation.content_grounding import (
    evaluate_claim_grounding,
    resolve_claim_grounding_policy,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.quality_bar_authority import (
    RequiredBarAuthority,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_input import (
    ModelQualityGateInput,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result import (
    ModelQualityGateResult,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    resolve_task_class_dod_checks,
)

pytestmark = pytest.mark.unit

# Minimal excerpt preserving the receipt's report boundary, identifier placement,
# and later blocker lines. No credentials or machine-specific paths are needed.
SOURCE = """Summarize the following final lane reports.
=== OMN-19739 is only partly done
OMN-19739 is only partly done. The roots file reads back on h201 only,
so omniclaude#2563 stays draft and I did not hand it off.

**By host**
- **h201:** initialised with git 2.50.1, readback exit 0.
- **h101:** blocked. It has git 2.39.5 and no newer git.
- **h202:** blocked. It has git 2.43.0.
=== studio-weight-r2
Reconciled the isolated lab venv using the fix from omnimarket#3481.
Focused suites passed 20/45/6/72 tests.
"""


def test_faithful_summary_grounds_blocker_in_the_same_report() -> None:
    content = "OMN-19739 BLOCKED omniclaude#2563 h101 and h202 blocked by git version."
    deterministic, heuristic = resolve_task_class_dod_checks("summarization", SOURCE)
    result = delta(
        ModelQualityGateInput(
            correlation_id=uuid4(),
            task_type="summarization",
            llm_response_content=content,
            dod_deterministic=deterministic,
            dod_heuristic=heuristic,
        ),
        grounding_source=SOURCE,
    )
    assert result.passed, result.failure_reasons
    assert any(
        row.rule == "claims_grounded" and row.passed for row in result.rule_evaluations
    )


@pytest.mark.parametrize(
    "answer",
    [
        "omnimarket#3481 is blocked.",
        "OMN-19739 was cancelled.",
        "omniclaude#2563 was deprioritized.",
    ],
)
def test_a_neighboring_report_cannot_supply_the_claimed_state(answer: str) -> None:
    verdict = evaluate_claim_grounding(
        content=answer,
        grounding_source=SOURCE,
        policy=resolve_claim_grounding_policy(),
    )
    assert verdict.ungrounded


def _reason(failure: str) -> str:
    return HandlerDelegationWorkflow._score_vs_bar_reason(
        ModelQualityGateResult(
            correlation_id=uuid4(),
            passed=False,
            quality_score=0.0,
            failure_reasons=(failure,),
            fallback_recommended=True,
        ),
        RequiredBarAuthority(
            required_bar=0.8,
            authority_source="task_class:summarization",
            score_source="quality_gate_graded_score",
        ),
        pre_filter_rejected=False,
    )


def test_generated_gate_reason_survives_terminal_error_redaction() -> None:
    reason = _reason("UNGROUNDED: blocked (OMN-19739)")
    assert sanitize_error_string(reason) == reason
    assert "task_class:summarization" in reason


@pytest.mark.parametrize(
    "failure",
    [
        "password=" + "synthetic-test-value",
        "Bearer " + "synthetic-test-value",
        "postgresql://test:synthetic-test-value@localhost/db",
        "-----BEGIN PRIVATE KEY----- synthetic-test-value",
    ],
)
def test_true_secret_control_still_redacts_the_whole_terminal_reason(
    failure: str,
) -> None:
    assert (
        sanitize_error_string(_reason(failure))
        == "[REDACTED - potentially sensitive data]"
    )
