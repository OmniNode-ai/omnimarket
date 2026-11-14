# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-24296: the task-kind guard's error chain."""

from __future__ import annotations

from typing import Any

import pytest
from omnibase_infra.errors import ProtocolConfigurationError
from pydantic import ValidationError

from omnimarket.models.delegation.model_delegation_routing_overlay import (
    EMPTY_DELEGATION_ROUTING_OVERLAY,
    ModelDelegationRoutingOverlay,
)
from omnimarket.nodes.node_delegation_task_kind_guard_compute.handlers.handler_task_kind_guard import (
    HandlerDelegationTaskKindGuard,
)
from omnimarket.nodes.node_delegation_task_kind_guard_compute.models.enum_task_kind_guard import (
    EnumTaskKindGuardReason,
    EnumTaskKindGuardRefusal,
    EnumTaskKindGuardRoute,
)
from omnimarket.nodes.node_delegation_task_kind_guard_compute.models.model_task_kind_guard_request import (
    ModelTaskKindGuardRequest,
)
from omnimarket.nodes.node_delegation_task_kind_guard_compute.models.model_task_kind_guard_tally import (
    ModelTaskKindGuardTally,
)

pytestmark = pytest.mark.unit

LOCAL_URL = "http://127.0.0.1:8000/v1/chat/completions"


def _overlay_doc() -> dict[str, Any]:
    return {
        "schema_version": "delegation_routing_overlay.v1",
        "escalation_chains": [
            {
                "task_class": "reasoning",
                "escalate_on": "acceptance_check",
                "rungs": ["local"],
            }
        ],
        "local_endpoints": {
            "backends": [
                {
                    "backend_id": "local-coder",
                    "provider": "local",
                    "tier": "local",
                    "endpoint_url": LOCAL_URL,
                    "model_name": "m",
                }
            ]
        },
        "task_kind_rungs": {
            "routes": [
                {
                    "task_class": "reasoning",
                    "task_kind": "ci_red_triage",
                    "local_backend_id": "local-coder",
                    "min_f1": 0.8,
                    "guard": {
                        "min_samples": 4,
                        "accuracy_lower_bound": 0.6,
                        "measured_correct": 123,
                        "measured_n": 165,
                    },
                    "kinds": {
                        "base-red": {
                            "rung": "local",
                            "input": "facts",
                            "n": 20,
                            "f1": 0.92,
                        },
                        "product": {
                            "rung": "current",
                            "input": "facts",
                            "n": 20,
                            "f1": 0.49,
                        },
                    },
                }
            ]
        },
    }


def _handler() -> HandlerDelegationTaskKindGuard:
    overlay = ModelDelegationRoutingOverlay.model_validate(_overlay_doc())
    return HandlerDelegationTaskKindGuard(load_overlay=lambda: overlay)


def test_an_unbound_overlay_is_refused() -> None:
    handler = HandlerDelegationTaskKindGuard(
        load_overlay=lambda: EMPTY_DELEGATION_ROUTING_OVERLAY
    )
    request = ModelTaskKindGuardRequest(
        task_class="reasoning",
        task_kind="ci_red_triage",
        answered_kind="base-red",
        tally=ModelTaskKindGuardTally(scored=2, correct=1),
    )
    answer = handler.handle(request)
    assert answer.ok is False
    assert answer.refusal is EnumTaskKindGuardRefusal.OVERLAY_UNBOUND
    assert answer.route is None
    assert answer.detail
    assert answer.tally.scored == 2
    assert answer.tally.correct == 1


def test_a_loader_that_raises_is_refused_as_overlay_invalid() -> None:
    def _raise() -> ModelDelegationRoutingOverlay:
        raise ProtocolConfigurationError("the overlay file is missing")

    handler = HandlerDelegationTaskKindGuard(load_overlay=_raise)
    request = ModelTaskKindGuardRequest(
        task_class="reasoning",
        task_kind="ci_red_triage",
        answered_kind="base-red",
    )
    answer = handler.handle(request)
    assert answer.ok is False
    assert answer.refusal is EnumTaskKindGuardRefusal.OVERLAY_INVALID
    assert "the overlay file is missing" in answer.detail


def test_an_unknown_task_kind_is_refused_as_no_route() -> None:
    handler = _handler()
    request = ModelTaskKindGuardRequest(
        task_class="reasoning",
        task_kind="no-such-kind",
        answered_kind="base-red",
    )
    answer = handler.handle(request)
    assert answer.ok is False
    assert answer.refusal is EnumTaskKindGuardRefusal.NO_ROUTE
    assert answer.route is None
    assert answer.detail


def test_an_answer_that_disagrees_with_the_deterministic_class_escalates() -> None:
    handler = _handler()
    request = ModelTaskKindGuardRequest(
        task_class="reasoning",
        task_kind="ci_red_triage",
        answered_kind="base-red",
        deterministic_kind="product",
    )
    answer = handler.handle(request)
    assert answer.ok is True
    assert answer.route is EnumTaskKindGuardRoute.ESCALATE
    assert answer.reason is EnumTaskKindGuardReason.DETERMINISTIC_DISAGREES


def test_an_answer_kind_absent_from_the_overlay_escalates() -> None:
    handler = _handler()
    request = ModelTaskKindGuardRequest(
        task_class="reasoning",
        task_kind="ci_red_triage",
        answered_kind="no-such-kind",
        deterministic_kind="no-such-kind",
    )
    answer = handler.handle(request)
    assert answer.ok is True
    assert answer.route is EnumTaskKindGuardRoute.ESCALATE
    assert answer.reason is EnumTaskKindGuardReason.KIND_UNDECLARED


def test_a_guard_tripped_tally_escalates_even_when_the_answer_agrees() -> None:
    handler = _handler()
    request = ModelTaskKindGuardRequest(
        task_class="reasoning",
        task_kind="ci_red_triage",
        answered_kind="base-red",
        deterministic_kind="base-red",
        tally=ModelTaskKindGuardTally(scored=3, correct=1),
    )
    answer = handler.handle(request)
    assert answer.ok is True
    assert answer.route is EnumTaskKindGuardRoute.ESCALATE
    assert answer.reason is EnumTaskKindGuardReason.GUARD_TRIPPED
    assert answer.tally.scored == 4
    assert answer.tally.correct == 2
    assert answer.accuracy == pytest.approx(0.5)


def test_a_tally_at_or_above_the_bound_is_accepted() -> None:
    handler = _handler()
    request = ModelTaskKindGuardRequest(
        task_class="reasoning",
        task_kind="ci_red_triage",
        answered_kind="base-red",
        deterministic_kind="base-red",
        tally=ModelTaskKindGuardTally(scored=3, correct=2),
    )
    answer = handler.handle(request)
    assert answer.ok is True
    assert answer.route is EnumTaskKindGuardRoute.LOCAL
    assert answer.reason is EnumTaskKindGuardReason.LOCAL_ACCEPTED
    assert answer.tally.scored == 4
    assert answer.tally.correct == 3
    assert answer.accuracy == pytest.approx(0.75)


def test_a_request_with_correct_above_scored_fails_validation() -> None:
    with pytest.raises(ValidationError, match="exceeds"):
        ModelTaskKindGuardRequest(
            task_class="reasoning",
            task_kind="ci_red_triage",
            answered_kind="base-red",
            tally=ModelTaskKindGuardTally(scored=1, correct=2),
        )
