# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Assert complete escalation verdicts when multiple terminal conditions apply."""

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_delegation_escalation_decision_compute.handlers.handler_escalation_decision import (
    HandlerEscalationDecision,
)
from omnimarket.routing.model_escalation_decision_request import (
    ModelEscalationDecisionRequest,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, (True, "cloud", None)),
        (
            {
                "error_retryable": False,
                "escalation_count": 2,
                "current_tier_name": None,
                "next_tier_name": None,
            },
            (False, None, "upstream_refused"),
        ),
        (
            {"escalation_count": 2, "current_tier_name": None, "next_tier_name": None},
            (False, None, "max_escalation_attempts_reached"),
        ),
        (
            {"current_tier_name": None, "next_tier_name": None},
            (False, None, "current_tier_unknown"),
        ),
        (
            {
                "next_tier_name": None,
                "no_higher_tier_reason": "no_higher_tier_available: cloud excluded",
            },
            (False, None, "no_higher_tier_available: cloud excluded"),
        ),
        (
            {"max_escalation_attempts": 0},
            (False, None, "max_escalation_attempts_reached"),
        ),
        (
            {"escalation_count": 1, "no_higher_tier_reason": "unused"},
            (True, "cloud", None),
        ),
    ],
    ids=[
        "escalate",
        "non-retryable-first",
        "budget-first",
        "unknown-tier-first",
        "ladder-exhausted",
        "zero-budget",
        "last-budget-slot",
    ],
)
def test_complete_verdict_and_precedence(
    overrides: dict[str, object], expected: tuple[bool, str | None, str | None]
) -> None:
    fields: dict[str, object] = {
        "escalation_count": 0,
        "max_escalation_attempts": 2,
        "current_tier_name": "local",
        "error_retryable": True,
        "next_tier_name": "cloud",
        "non_retryable_reason": "upstream_refused",
    }
    fields.update(overrides)
    result = HandlerEscalationDecision().handle(
        ModelEscalationDecisionRequest.model_validate(fields)
    )
    assert (
        result.can_escalate,
        result.next_tier_name,
        result.terminal_failure_reason,
    ) == expected


def test_verdict_fields_match_the_declared_terminal_contract() -> None:
    contract_path = (
        Path(__file__).parents[3]
        / "src/omnimarket/nodes"
        / "node_delegation_escalation_decision_compute/contract.yaml"
    )
    contract = yaml.safe_load(contract_path.read_text())
    handler = HandlerEscalationDecision()
    result = handler.handle(
        ModelEscalationDecisionRequest(
            escalation_count=0,
            max_escalation_attempts=2,
            current_tier_name="local",
            next_tier_name="cloud",
        )
    )
    assert set(result.model_dump()) == set(contract["outputs"])
    assert contract["handler"]["class"] == type(handler).__name__
    assert contract["terminal_event"] == (
        "onex.evt.omnimarket.delegation-escalation-decided.v1"
    )
    assert contract["event_bus"]["publish_topics"] == [contract["terminal_event"]]
