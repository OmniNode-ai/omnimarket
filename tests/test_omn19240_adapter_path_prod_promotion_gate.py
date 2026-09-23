# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The gate-decision fold is reachable through the REAL adapter path (OMN-19240).

WHY THIS FILE EXISTS, AND WHY IT IS NOT ANOTHER FOLD TEST
---------------------------------------------------------
``tests/test_omn18999_prod_promotion_gate_projection.py`` drives the WRITER,
which builds the fold's request by hand, and it passed green while every live
gate decision on the ``.201`` dev lane was dead-lettered. It could not see the
defect for the same reason the lab lane-health fold tests could not see
theirs (OMN-18769): the shared ``runtime_local_adapter`` builds a def-B
handler's input from the UNWRAPPED domain payload of the bus message, and the
request model declared ``{event, fallback_correlation_id, source_topic}``, a
wrapper the adapter has no way to construct. Every real message failed with
``11 validation errors for ModelProdPromotionGateProjectionRequest`` (``event``
required, and each of the decision's ten fields an extra input) before the fold
ran, and the combined dispatch was routed to the dead-letter queue, from which
the replay loop fed it back roughly 22,900 times.

The payload below is a verbatim capture of the ``payload`` of that envelope
(``e8fbd49f-5b02-5745-84ff-e54332c613cd``, correlation ``2d3ccad1``), read off
``onex.dlq.omnibase-infra.events.v1`` on the ``.201`` dev lane with ``rpk topic
consume`` on 2026-09-23. It is not a hand-written fixture; a hand-written
fixture is what hid this defect.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from omnibase_core.runtime.runtime_local_adapter import _invoke_handle_method
from pydantic import ValidationError

from omnimarket.nodes.node_projection_prod_promotion_gate.handlers.handler_projection_prod_promotion_gate import (
    HandlerProjectionProdPromotionGate,
)
from omnimarket.nodes.node_projection_prod_promotion_gate.models import (
    ModelProdPromotionGateProjectionRequest,
    ModelProdPromotionGateProjectionResult,
)

# Captured from onex.dlq.omnibase-infra.events.v1: the `payload` of
# original_message.value, original_topic
# onex.evt.omnimarket.prod-promotion-gate-evaluated.v1, original offset 25384.
_REAL_DECISION: dict[str, Any] = {
    "allowed": True,
    "image_digest": None,
    "rollback_target": "omninode-runtime:v2.3.1",
    "reason": "dev lane is not gated; deploy may proceed",
    "deploy_context": {
        "scope": "full",
        "git_ref": "c159b711808d1eca01accabe8e3b1f3db3f3cdf9",
        "runtime_lane": "dev",
        "build_source": "workspace",
        "services": [],
        "image_ref": None,
        "image_digest": None,
        "promotion_batch_id": None,
        "requested_by": "gha/omnibase_infra/pr-3996",
        "smoke_test": False,
        "previous_image": "omninode-runtime:v2.3.1",
        "rollback_target": None,
    },
    "outcome": "allowed_lane_not_gated",
    "grant_id": None,
    "requested_image_digest": None,
    "evaluated_at": None,
    "correlation_id": "2d3ccad1-6d0d-4f61-9f1d-5c5a8bd12d90",
}


@pytest.mark.unit
def test_the_adapter_can_build_the_request_from_the_real_decision() -> None:
    """This is the exact call the adapter makes. It raised 11 errors before."""
    request = ModelProdPromotionGateProjectionRequest.model_validate(_REAL_DECISION)

    assert request.allowed is True
    assert request.correlation_id == UUID(_REAL_DECISION["correlation_id"])


@pytest.mark.unit
def test_the_real_decision_reaches_handle_through_the_adapters_own_invoker() -> None:
    """Drive ``_invoke_handle_method`` itself, not a local imitation of it.

    Importing the adapter's helper is deliberate: a reimplementation here would
    drift from the runtime and could go green while the lane stayed broken.
    Asserting on the row, not merely on the call returning, proves the fold ran.
    """
    result = _invoke_handle_method(
        HandlerProjectionProdPromotionGate().handle, dict(_REAL_DECISION)
    )

    assert isinstance(result, ModelProdPromotionGateProjectionResult)
    row = result.row
    assert row.correlation_id == UUID(_REAL_DECISION["correlation_id"])
    assert row.outcome == "allowed_lane_not_gated"
    assert row.allowed is True
    assert row.runtime_lane == "dev"
    assert row.rollback_target == "omninode-runtime:v2.3.1"


@pytest.mark.unit
def test_runtime_injected_keys_do_not_turn_a_decision_into_a_dead_letter() -> None:
    """Delivery keys the runtime may add beside the payload are not the decision.

    A key a later producer or the transport adds must not flip a readable
    decision into a DLQ entry -- the same tolerance the wire model already has.
    """
    enriched = dict(_REAL_DECISION)
    enriched["_topic"] = "onex.evt.omnimarket.prod-promotion-gate-evaluated.v1"
    enriched["a_field_a_later_producer_adds"] = "x"

    result = _invoke_handle_method(
        HandlerProjectionProdPromotionGate().handle, enriched
    )

    assert isinstance(result, ModelProdPromotionGateProjectionResult)
    assert result.row.correlation_id == UUID(_REAL_DECISION["correlation_id"])


@pytest.mark.unit
def test_a_decision_with_no_correlation_and_no_delivery_fallback_still_folds() -> None:
    """The adapter path supplies no delivery coordinate, so the key is derived.

    A decision minted before OMN-18999 carries no correlation. On the writer
    path the delivery's own coordinates key it; on the adapter path there are
    none, and raising here would dead-letter every such decision exactly as the
    wrapper did. The derived key is content-addressed, so a redelivery of the
    same decision converges and a different decision does not collide.
    """
    legacy = {"allowed": False, "reason": "OCC evidence is not durable (pending)"}
    other = {"allowed": False, "reason": "readiness projection is not READY"}
    handle = HandlerProjectionProdPromotionGate().handle

    first = _invoke_handle_method(handle, dict(legacy))
    again = _invoke_handle_method(handle, dict(legacy))
    different = _invoke_handle_method(handle, dict(other))

    assert isinstance(first, ModelProdPromotionGateProjectionResult)
    assert isinstance(again, ModelProdPromotionGateProjectionResult)
    assert isinstance(different, ModelProdPromotionGateProjectionResult)
    assert first.row.correlation_id == again.row.correlation_id
    assert first.row.correlation_id != different.row.correlation_id


@pytest.mark.unit
@pytest.mark.parametrize(
    "not_a_decision",
    [
        # A runtime-health snapshot: a real event on a sibling topic.
        {
            "correlation_id": "8a9ec0de-0000-4000-8000-000000000000",
            "lane": "compose-dev",
            "timestamp": "2026-09-20T07:00:00+00:00",
            "status": "DEGRADED",
        },
        # The wrapper shape itself: the decision nested one level down is not
        # a decision at the top level, and must not be half-read as one.
        {"event": dict(_REAL_DECISION)},
        {},
    ],
    ids=["runtime-health", "wrapper-shape", "empty"],
)
def test_a_payload_that_is_not_a_gate_decision_is_refused(
    not_a_decision: dict[str, Any],
) -> None:
    """``allowed`` is the one field every decision has always carried."""
    with pytest.raises(ValidationError, match="allowed"):
        ModelProdPromotionGateProjectionRequest.model_validate(not_a_decision)

    with pytest.raises(ValidationError, match="allowed"):
        _invoke_handle_method(
            HandlerProjectionProdPromotionGate().handle, dict(not_a_decision)
        )
