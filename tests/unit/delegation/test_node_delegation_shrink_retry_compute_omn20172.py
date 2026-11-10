# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20172: node_delegation_shrink_retry_compute, its contract, a golden and an error event.

The node is a pure decision: the request carries the ladder's whole state and the
decision names the rung and its reason. The contract declares the one command topic
and the one terminal event; the golden test walks a request through the handler as
the node's payload would and reads the terminal event's fields, the error test shows
a request the model refuses before any decision is made.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.delegation.model_shrink_retry import (
    EnumShrinkRetryReason,
    EnumShrinkRetryRung,
    EnumShrinkRetryScope,
    ModelShrinkRetryCaps,
    ModelShrinkRetryDecision,
    ModelShrinkRetryDecisionRequest,
)
from omnimarket.nodes.node_delegation_shrink_retry_compute.handlers.handler_shrink_retry_policy import (
    HandlerShrinkRetryPolicy,
)

pytestmark = pytest.mark.unit

NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_delegation_shrink_retry_compute"
)
COMMAND_TOPIC = "onex.cmd.omnimarket.delegation-shrink-retry-requested.v1"
TERMINAL_TOPIC = "onex.evt.omnimarket.delegation-shrink-retry-decided.v1"
CAPS = ModelShrinkRetryCaps(
    max_config_swaps=1,
    max_split_depth=2,
    unit_attempts_per_unit=2,
    unit_prompt="One part of a larger request.\n\n{source}",
)


def _contract() -> dict[str, object]:
    return yaml.safe_load((NODE_DIR / "contract.yaml").read_text())


def test_contract_declares_the_command_and_the_terminal_event() -> None:
    contract = _contract()
    bus = contract["event_bus"]
    assert isinstance(bus, dict)
    assert bus["subscribe_topics"] == [COMMAND_TOPIC]
    assert bus["publish_topics"] == [TERMINAL_TOPIC]
    assert contract["terminal_event"] == TERMINAL_TOPIC
    assert contract["name"] == "node_delegation_shrink_retry_compute"


def test_golden_whole_failure_on_a_decomposable_task_shrinks() -> None:
    request = ModelShrinkRetryDecisionRequest(
        task_id="golden-1",
        task_class="summarization",
        scope=EnumShrinkRetryScope.WHOLE,
        caps=CAPS,
        splittable=True,
    )
    decision = HandlerShrinkRetryPolicy().handle(request)
    event = decision.model_dump(mode="json")
    assert event["task_id"] == "golden-1"
    assert event["rung"] == EnumShrinkRetryRung.SHRINK.value
    assert event["reason"] == EnumShrinkRetryReason.DECOMPOSABLE.value
    assert event["configuration"] is None
    assert ModelShrinkRetryDecision.model_validate(event) == decision


def test_error_event_a_unit_failure_without_its_unit_count_is_refused() -> None:
    with pytest.raises(ValidationError, match="unit count"):
        ModelShrinkRetryDecisionRequest(
            task_id="error-1",
            task_class="summarization",
            scope=EnumShrinkRetryScope.UNIT,
            caps=CAPS,
        )


def test_error_event_a_class_with_no_caps_escalates_instead_of_shrinking() -> None:
    request = ModelShrinkRetryDecisionRequest(
        task_id="error-2",
        task_class="summarization",
        scope=EnumShrinkRetryScope.WHOLE,
        caps=None,
        splittable=True,
    )
    event = HandlerShrinkRetryPolicy().handle(request).model_dump(mode="json")
    assert event["task_id"] == "error-2"
    assert event["rung"] == EnumShrinkRetryRung.ESCALATE_WHOLE.value
    assert event["reason"] == EnumShrinkRetryReason.SHRINK_NOT_DECLARED.value
