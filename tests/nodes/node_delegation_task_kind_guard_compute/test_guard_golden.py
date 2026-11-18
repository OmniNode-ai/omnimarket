# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-24296: the task-kind guard's golden chain."""

from __future__ import annotations

from typing import Any

import pytest
import yaml

from omnimarket.models.delegation.model_delegation_routing_overlay import (
    ModelDelegationRoutingOverlay,
)
from omnimarket.nodes.node_delegation_task_kind_guard_compute.handlers.handler_task_kind_guard import (
    HandlerDelegationTaskKindGuard,
)
from omnimarket.nodes.node_delegation_task_kind_guard_compute.models.enum_task_kind_guard import (
    EnumTaskKindGuardReason,
    EnumTaskKindGuardRoute,
)
from omnimarket.nodes.node_delegation_task_kind_guard_compute.models.model_task_kind_guard_request import (
    ModelTaskKindGuardRequest,
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


def test_a_local_kind_that_agrees_is_accepted_on_the_local_rung() -> None:
    handler = _handler()
    request = ModelTaskKindGuardRequest(
        task_class="reasoning",
        task_kind="ci_red_triage",
        answered_kind="base-red",
        deterministic_kind="base-red",
    )
    answer = handler.handle(request)
    assert answer.ok is True
    assert answer.route is EnumTaskKindGuardRoute.LOCAL
    assert answer.reason is EnumTaskKindGuardReason.LOCAL_ACCEPTED
    assert answer.tally.scored == 1
    assert answer.tally.correct == 1
    assert answer.scored_correct is True
    assert answer.local_backend_id == "local-coder"


def test_a_current_kind_stays_on_its_current_rung() -> None:
    handler = _handler()
    request = ModelTaskKindGuardRequest(
        task_class="reasoning",
        task_kind="ci_red_triage",
        answered_kind="product",
        deterministic_kind="product",
    )
    answer = handler.handle(request)
    assert answer.ok is True
    assert answer.route is EnumTaskKindGuardRoute.ESCALATE
    assert answer.reason is EnumTaskKindGuardReason.KIND_STAYS_ON_CURRENT


def test_an_answer_with_no_deterministic_class_leaves_the_tally_unchanged() -> None:
    handler = _handler()
    request = ModelTaskKindGuardRequest(
        task_class="reasoning",
        task_kind="ci_red_triage",
        answered_kind="base-red",
    )
    answer = handler.handle(request)
    assert answer.ok is True
    assert answer.route is EnumTaskKindGuardRoute.LOCAL
    assert answer.reason is EnumTaskKindGuardReason.LOCAL_ACCEPTED
    assert answer.tally.scored == 0
    assert answer.tally.correct == 0
    assert answer.scored_correct is None


def test_the_contract_names_the_handler_and_declares_no_topic() -> None:
    import pathlib

    contract_path = (
        pathlib.Path(__file__).resolve().parents[3]
        / "src/omnimarket/nodes/node_delegation_task_kind_guard_compute/contract.yaml"
    )
    contract = yaml.safe_load(contract_path.read_text())
    assert contract["handler"]["class"] == "HandlerDelegationTaskKindGuard"
    operations = {
        h["operation"]: h["handler"]["name"]
        for h in contract["handler_routing"]["handlers"]
    }
    assert operations == {"guard_task_kind_answer": "HandlerDelegationTaskKindGuard"}
    assert contract["event_bus"] == {"subscribe_topics": [], "publish_topics": []}
    assert contract["runtime_dispatch"]["external_trigger"]
