# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""Claim grounding in the delegation quality gate (OMN-19199).

RED-first anchor: operator run ``844cac18``, a ``summarization`` delegation over
three ledger rows. Its third bullet says the "third lane (OMN-19174) is currently
deprioritized to accommodate the producer work". OMN-19174 occurs in the input
only inside one row's cost sentence, and no row says anything is deprioritized.
Every identifier in the answer is real, so identifier grounding passes it, and
the gate scored it ``passed=true score=1.0``.

The faithful summary of the same three rows is the control and must still pass.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from omnibase_core.models.delegation.wire import ModelQualityGateIntent

from omnimarket.delegation.content_grounding import (
    evaluate_claim_grounding,
    resolve_claim_grounding_policy,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate_intent import (
    HandlerQualityGateIntent,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_input import (
    ModelQualityGateInput,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    resolve_task_class_dod_checks,
)

pytestmark = pytest.mark.unit

_FIXTURES = Path(__file__).parents[2] / "fixtures" / "delegation" / "omn19199"


def _source() -> str:
    return (_FIXTURES / "r844cac18_source.txt").read_text()


def _gate_input(content: str) -> ModelQualityGateInput:
    deterministic, heuristic = resolve_task_class_dod_checks("summarization", _source())
    return ModelQualityGateInput(
        correlation_id=uuid4(),
        task_type="summarization",
        llm_response_content=content,
        dod_deterministic=deterministic,
        dod_heuristic=heuristic,
    )


def test_recorded_summary_that_invents_a_claim_about_a_real_identifier_fails() -> None:
    content = (_FIXTURES / "r844cac18_invented_response.txt").read_text()
    result = delta(_gate_input(content), grounding_source=_source())

    assert result.passed is False
    assert result.fail_category == "fail_heuristic"
    assert any(
        reason.startswith("UNGROUNDED:") and "deprioritized" in reason
        for reason in result.failure_reasons
    ), result.failure_reasons
    assert any(
        row.startswith("claim:deprioritized") for row in result.ungrounded_identifiers
    ), result.ungrounded_identifiers


def test_faithful_summary_of_the_same_rows_still_passes() -> None:
    content = (_FIXTURES / "r844cac18_faithful_response.txt").read_text()
    result = delta(_gate_input(content), grounding_source=_source())

    assert result.passed is True, result.failure_reasons
    assert "claims_grounded" not in result.skipped_checks
    assert any(
        evaluation.rule == "claims_grounded" and evaluation.passed
        for evaluation in result.rule_evaluations
    )


def test_without_a_source_the_check_is_skipped_and_recorded_never_passed() -> None:
    content = (_FIXTURES / "r844cac18_invented_response.txt").read_text()
    result = delta(_gate_input(content))

    assert "claims_grounded" in result.skipped_checks
    assert all(
        evaluation.rule != "claims_grounded" for evaluation in result.rule_evaluations
    )


def test_a_stamped_bus_payload_reaches_the_check_through_the_intent_handler() -> None:
    class _Stamped(ModelQualityGateInput):
        grounding_source: str | None = None

    deterministic, heuristic = resolve_task_class_dod_checks("summarization", _source())
    stamped = _Stamped(
        correlation_id=uuid4(),
        task_type="summarization",
        llm_response_content=(
            _FIXTURES / "r844cac18_invented_response.txt"
        ).read_text(),
        dod_deterministic=deterministic,
        dod_heuristic=heuristic,
        grounding_source=_source(),
    )

    result = HandlerQualityGateIntent().handle(ModelQualityGateIntent(payload=stamped))

    assert result.passed is False


def test_the_summarization_class_binds_the_check_in_its_contract() -> None:
    _, heuristic = resolve_task_class_dod_checks("summarization", _source())

    assert resolve_claim_grounding_policy().check_name in heuristic


@pytest.mark.parametrize(
    ("source", "answer"),
    [
        # The stated state occurs in the rows of the cited identifier.
        ("OMN-1001 | CLAIM | blocked on review", "OMN-1001 is blocked."),
        # A synonym in the same declared group grounds it.
        ("OMN-1001 | TERMINAL | outcome=DONE", "OMN-1001 was completed."),
        # A clause that cites no identifier the source holds is not checked.
        ("no ids here, just lane a", "Lane a is blocked."),
        # A clause that asserts no declared state is not a claim.
        ("OMN-1001 | CLAIM | lane a", "OMN-1001 names lane a."),
    ],
)
def test_supported_or_stateless_clauses_are_not_refused(
    source: str, answer: str
) -> None:
    verdict = evaluate_claim_grounding(
        content=answer,
        grounding_source=source,
        policy=resolve_claim_grounding_policy(),
    )

    assert verdict.evaluated is True
    assert verdict.ungrounded == ()


@pytest.mark.parametrize(
    ("source", "answer", "group"),
    [
        # State stated, and the cited identifier's row does not carry it even
        # though another row does: the row scope is the point.
        (
            "OMN-1001 | CLAIM | lane a\nOMN-1002 | CLAIM | paused for review",
            "OMN-1001 is paused.",
            "deprioritized",
        ),
        ("OMN-1001 | CLAIM | lane a", "OMN-1001 was cancelled.", "cancelled"),
        # The state sits after a comma that follows the identifier; it is still
        # about that identifier (live run 09e84a07, the bypass of the first cut).
        (
            "OMN-1001 | CLAIM | lane a",
            "The lane in parentheses, (OMN-1001), is currently deprioritized.",
            "deprioritized",
        ),
    ],
)
def test_an_unsupported_state_is_reported(source: str, answer: str, group: str) -> None:
    verdict = evaluate_claim_grounding(
        content=answer,
        grounding_source=source,
        policy=resolve_claim_grounding_policy(),
    )

    assert verdict.evaluated is True
    assert [item.group for item in verdict.ungrounded] == [group]


def test_an_identifier_never_carries_into_the_next_statement() -> None:
    verdict = evaluate_claim_grounding(
        content="OMN-1001 names lane a.\nThe app is blocked.",
        grounding_source="OMN-1001 | CLAIM | lane a",
        policy=resolve_claim_grounding_policy(),
    )

    assert verdict.evaluated is True
    assert verdict.ungrounded == ()
