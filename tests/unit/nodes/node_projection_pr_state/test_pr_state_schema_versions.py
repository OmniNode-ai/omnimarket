# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every consumer of pr-state-observed accepts both schema versions."""

from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_pr_handoff_orchestrator.models.model_pr_handoff_observation_ingress import (
    ModelPrHandoffObservationIngress,
)
from omnimarket.nodes.node_projection_pr_state.handlers.pr_state_fold import (
    HandlerProjectionPrState,
)
from omnimarket.nodes.node_projection_pr_state.models.model_pr_state_fold_request import (
    ModelPrStateFoldRequest,
)
from tests.unit.nodes.node_pr_state_emit_effect.helpers import event, event_v2

pytestmark = pytest.mark.unit


def enriched(e: Any) -> dict[str, Any]:
    return {**e.model_dump(mode="json"), "actor": "watcher", "lane": "dev"}


@pytest.mark.parametrize("make", [event, event_v2], ids=["v1", "v2"])
def test_projection_folds_either_version(make: Any) -> None:
    e = make()
    request = ModelPrStateFoldRequest.model_validate(enriched(e))
    assert request.event == e
    result = HandlerProjectionPrState().handle(request)
    assert result.event.digest == e.digest


def test_projection_refuses_a_version_2_payload_whose_digest_is_wrong() -> None:
    wire = enriched(event_v2())
    wire["digest"] = "0" * 64
    with pytest.raises(ValidationError, match="digest does not match"):
        ModelPrStateFoldRequest.model_validate(wire)


@pytest.mark.parametrize("make", [event, event_v2], ids=["v1", "v2"])
def test_handoff_ingress_takes_either_version(make: Any) -> None:
    e = make()
    ingress = ModelPrHandoffObservationIngress.model_validate(e.model_dump(mode="json"))
    assert ingress.repo == e.repo
    assert ingress.observation().red_contexts == e.red_contexts
