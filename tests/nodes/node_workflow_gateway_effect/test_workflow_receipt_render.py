# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The receipt renderer and its byte-pinned terminal-event hash.

Exemplar: tests/nodes/node_plan_list/test_http_plans.py (typed answers); here at the pure seam.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID

import pytest

from omnimarket.nodes.node_workflow_gateway_effect.handlers.workflow_receipt_render import (
    WorkflowReceiptNotTerminalError,
    decode_rule_evaluations,
    render_workflow_receipt,
)
from omnimarket.nodes.node_workflow_gateway_effect.models.model_workflow_store import (
    ModelWorkflowRow,
)
from tests.nodes.node_workflow_gateway_effect.fakes import SKILL_TOPIC

pytestmark = pytest.mark.unit
SUBMITTED = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
FINISHED = datetime(2026, 11, 13, 12, 1, tzinfo=UTC)


def _row(**changes: object) -> ModelWorkflowRow:
    fields: dict[str, object] = {
        "workflow_id": UUID(int=1),
        "tenant_id": UUID(int=2),
        "workflow_type": "delegate-skill",
        "status": "completed",
        "envelope_id": UUID(int=3),
        "correlation_id": UUID(int=4),
        "command_topic": SKILL_TOPIC,
        "submitted_at": SUBMITTED,
        "updated_at": FINISHED,
        "terminal_model_used": "qwen",
        "terminal_total_tokens": 12,
        "terminal_latency_ms": 340,
        "result_content": "alive",
    }
    fields.update(changes)
    return ModelWorkflowRow.model_validate(fields)


def test_terminal_event_hash_is_the_pinned_five_field_shape() -> None:
    receipt = render_workflow_receipt(_row(), "ci-run-1")
    # Another repository's replay verifier mirrors exactly this key set; the test fails if it changes.
    pinned = {
        "correlation_id": str(UUID(int=4)),
        "status": "completed",
        "terminal_model_used": "qwen",
        "terminal_total_tokens": 12,
        "terminal_latency_ms": 340,
    }
    expected = sha256(
        json.dumps(pinned, sort_keys=True, default=str).encode()
    ).hexdigest()
    assert receipt.terminal_event_hash == expected
    assert receipt.verifier == "ci-run-1"
    assert receipt.completed_at == FINISHED
    assert receipt.event_count == 1
    assert len(receipt.projection_row_hash) == 64


def test_the_projection_hash_changes_with_the_row_and_not_with_the_verifier() -> None:
    base = render_workflow_receipt(_row(), "a")
    assert (
        render_workflow_receipt(_row(), "b").projection_row_hash
        == base.projection_row_hash
    )
    other = render_workflow_receipt(_row(result_content="different"), "a")
    assert other.projection_row_hash != base.projection_row_hash
    assert other.terminal_event_hash == base.terminal_event_hash  # result is not in it


def test_a_failed_row_carries_its_failure_attribution_and_rule_evidence() -> None:
    receipt = render_workflow_receipt(
        _row(
            status="failed",
            terminal_failure_class="refused",
            terminal_failure_code="ONEX_X",
            terminal_failure_reason="no key",
            terminal_remediation="register a key",
            terminal_rule_evaluations=json.dumps(
                [
                    {
                        "rule": "concise",
                        "enforcement": "blocking",
                        "passed": False,
                        "detail": "long",
                    }
                ]
            ),
        ),
        "r",
    )
    assert receipt.status == "failed"
    assert receipt.terminal_failure_code == "ONEX_X"
    assert [e.rule for e in receipt.rule_evaluations] == ["concise"]


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"status": "published"}, id="not-terminal"),
        pytest.param({"terminal_model_used": None}, id="null-model"),
        pytest.param({"terminal_total_tokens": None}, id="null-tokens"),
        pytest.param({"terminal_latency_ms": None}, id="null-latency"),
    ],
)
def test_a_non_terminal_or_incomplete_row_has_no_receipt(
    changes: dict[str, object],
) -> None:
    with pytest.raises(WorkflowReceiptNotTerminalError):
        render_workflow_receipt(_row(**changes), "r")


def test_a_blank_verifier_is_refused() -> None:
    with pytest.raises(ValueError, match="verifier"):
        render_workflow_receipt(_row(), "  ")


def test_a_completed_row_that_names_a_failure_is_refused() -> None:
    with pytest.raises(ValueError, match="completed"):
        render_workflow_receipt(_row(terminal_failure_code="X"), "r")


def test_rule_evaluation_decoding() -> None:
    assert decode_rule_evaluations(None) == ()
    with pytest.raises(TypeError):
        decode_rule_evaluations('{"not": "a list"}')
