# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-24296: the task-kind-to-rung seam."""

from __future__ import annotations

import pathlib
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.delegation.model_delegation_routing_overlay import (
    ModelDelegationRoutingOverlay,
)
from omnimarket.models.delegation.model_task_kind_rungs_overlay import (
    EnumTaskKindRung,
)
from omnimarket.nodes.node_delegation_task_kind_guard_compute.handlers.handler_task_kind_guard import (
    HandlerDelegationTaskKindGuard,
)
from omnimarket.nodes.node_delegation_task_kind_guard_compute.models.enum_task_kind_guard import (
    EnumTaskKindGuardRoute,
)
from omnimarket.nodes.node_delegation_task_kind_guard_compute.models.model_task_kind_guard_request import (
    ModelTaskKindGuardRequest,
)

pytestmark = pytest.mark.unit

FIXTURE_PATH = (
    pathlib.Path(__file__).resolve().parents[3]
    / "tests/fixtures/task_kind_rungs/overlay.yaml"
)


def _load_overlay() -> ModelDelegationRoutingOverlay:
    doc: dict[str, Any] = yaml.safe_load(FIXTURE_PATH.read_text())
    return ModelDelegationRoutingOverlay.model_validate(doc)


def test_every_kind_of_the_route_is_local_exactly_when_the_overlay_says_local() -> None:
    overlay = _load_overlay()
    assert overlay.task_kind_rungs is not None
    route = overlay.task_kind_rungs.route_for("reasoning", "ci_red_triage")
    assert route is not None
    handler = HandlerDelegationTaskKindGuard(load_overlay=lambda: overlay)
    for kind in route.kinds:
        request = ModelTaskKindGuardRequest(
            task_class="reasoning",
            task_kind="ci_red_triage",
            answered_kind=kind,
            deterministic_kind=kind,
        )
        answer = handler.handle(request)
        assert answer.ok is True
        expected = (
            EnumTaskKindGuardRoute.LOCAL
            if route.rung_for(kind) is EnumTaskKindRung.LOCAL
            else EnumTaskKindGuardRoute.ESCALATE
        )
        assert answer.route is expected, f"kind {kind!r} routed to {answer.route}"


def test_the_rung_enum_has_exactly_local_and_current() -> None:
    assert {rung.value for rung in EnumTaskKindRung} == {"local", "current"}


def test_an_overlay_value_of_a_rung_the_enum_does_not_know_is_refused() -> None:
    doc: dict[str, Any] = yaml.safe_load(FIXTURE_PATH.read_text())
    doc["task_kind_rungs"]["routes"][0]["kinds"]["base-red"]["rung"] = "paid"
    with pytest.raises(ValidationError):
        ModelDelegationRoutingOverlay.model_validate(doc)
