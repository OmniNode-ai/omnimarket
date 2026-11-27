# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A code answer's receipt names whether its tests ran: passed, failed or untested (OMN-26682)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from omnimarket.nodes.node_workflow_gateway_effect.handlers.workflow_receipt_render import (
    render_workflow_receipt,
)
from omnimarket.nodes.node_workflow_gateway_effect.models.model_workflow_store import (
    ModelWorkflowRow,
)
from tests.nodes.node_workflow_gateway_effect.fakes import SKILL_TOPIC

pytestmark = pytest.mark.unit

UNTESTED_DETAIL = (
    "UNTESTED: passes_existing_tests: the request named no tests, so the code "
    "answer was decided by its contract checks alone and was not run; name "
    "tests to have it graded on them"
)


def _row(rules: list[dict[str, object]] | None) -> ModelWorkflowRow:
    return ModelWorkflowRow.model_validate(
        {
            "workflow_id": UUID(int=1),
            "tenant_id": UUID(int=2),
            "workflow_type": "delegate-skill",
            "status": "completed",
            "envelope_id": UUID(int=3),
            "correlation_id": UUID(int=4),
            "command_topic": SKILL_TOPIC,
            "submitted_at": datetime(2026, 11, 25, 12, 0, tzinfo=UTC),
            "updated_at": datetime(2026, 11, 25, 12, 1, tzinfo=UTC),
            "terminal_model_used": "qwen",
            "terminal_total_tokens": 12,
            "terminal_latency_ms": 340,
            "result_content": "def f(): ...",
            "terminal_rule_evaluations": rules,
        }
    )


def test_no_tests_named_or_run_reads_untested() -> None:
    receipt = render_workflow_receipt(
        _row(
            [
                {
                    "rule": "named_tests_passed",
                    "enforcement": "scored",
                    "passed": False,
                    "detail": UNTESTED_DETAIL,
                }
            ]
        ),
        "ci",
    )
    assert receipt.status == "completed"
    assert receipt.tests_outcome == "untested"


def test_tests_that_ran_and_passed_read_passed() -> None:
    receipt = render_workflow_receipt(
        _row(
            [
                {
                    "rule": "named_tests_passed",
                    "enforcement": "blocking",
                    "passed": True,
                    "detail": None,
                }
            ]
        ),
        "ci",
    )
    assert receipt.tests_outcome == "passed"


def test_tests_that_ran_and_failed_read_failed() -> None:
    receipt = render_workflow_receipt(
        _row(
            [
                {
                    "rule": "named_tests_passed",
                    "enforcement": "blocking",
                    "passed": False,
                    "detail": "TESTS_FAILED: named tests failed: t::a",
                }
            ]
        ),
        "ci",
    )
    assert receipt.tests_outcome == "failed"


def test_an_answer_that_is_not_code_carries_no_tests_outcome() -> None:
    assert render_workflow_receipt(_row(None), "ci").tests_outcome is None
