# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Canonical def-B dispatch conformance for node_prod_promotion_gate_compute (OMN-14838).

Part of the Class-B Tier-1 canonical-shape fan-out (parent epic OMN-14355). The
handler was flipped from the def-A envelope shape
``handle(envelope: ModelEventEnvelope[Any]) -> ModelHandlerOutput[...]`` to the
canonical **definition B** shape
``handle(request: ModelProdPromotionGateCommand) -> ModelProdPromotionGateDecision``.
The business logic (``evaluate_gate``) is preserved byte-identical; only the thin
adapter boundary moved onto the shared runtime.

These tests drive the REAL shared runtime adapter
(``omnibase_core.runtime.runtime_local_adapter._invoke_handle_method``) — the same
call path the live runtime uses — over both the raw-dict payload (operation_match:
the adapter coerces the dict into the contract ``input_model``) and the concrete
model payload, and assert the typed ``ModelProdPromotionGateDecision`` comes back
directly (no ``ModelHandlerOutput`` wrapper, no per-node envelope handling).

RED->GREEN: on the PRE-flip def-A handler these tests are RED —
``_invoke_handle_method`` passes the raw dict to the ``envelope`` positional, whose
``.payload`` access explodes, and the direct-call test receives a wrapper object,
not a decision. On the def-B flip they are GREEN. ``test_handle_is_canonical_defb``
locks the shape so a regression back to the envelope core fails here.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from omnibase_core.runtime.runtime_local_adapter import _invoke_handle_method
from pydantic import BaseModel

from omnimarket.events.runtime_deployment import (
    EnumOccGateState,
    EnumProdGrantReason,
    EnumPromotionClass,
    EnumRuntimeLane,
    ModelProdPromotionGateDecision,
    ModelProdPromotionGrant,
    ModelReadinessProjectionFact,
)
from omnimarket.nodes.node_prod_promotion_gate_compute.handlers.handler_prod_promotion_gate import (
    HandlerProdPromotionGate,
)
from omnimarket.nodes.node_prod_promotion_gate_compute.models.model_prod_promotion_gate_command import (
    ModelProdPromotionGateCommand,
)

pytestmark = pytest.mark.unit

_DIGEST_STABILITY = "sha256:0037aaaa"
_BATCH = "promo-2026-06-02"
_ROLLBACK = "sha256:0036bbbb"
_REQUESTER = "node_redeploy_orchestrator"
_EVALUATED_AT = datetime(2026, 6, 21, 12, 0, 0, tzinfo=UTC)


def _grant(*, authorizes_candidate: bool = False) -> ModelProdPromotionGrant:
    return ModelProdPromotionGrant(
        grant_id="grant-omn-14838",
        approved_lane=EnumRuntimeLane.PROD,
        approved_image_digest=_DIGEST_STABILITY,
        approved_promotion_batch_id=_BATCH,
        approved_by="release-captain",
        created_at=_EVALUATED_AT - timedelta(minutes=5),
        expires_at=_EVALUATED_AT + timedelta(hours=2),
        authorizes_candidate=authorizes_candidate,
    )


def _ready() -> ModelReadinessProjectionFact:
    return ModelReadinessProjectionFact(
        runtime_lane=EnumRuntimeLane.STABILITY_TEST,
        readiness_state="READY",
        image_digest=_DIGEST_STABILITY,
        promotion_batch_id=_BATCH,
    )


def _command(
    *,
    projection: ModelReadinessProjectionFact | None,
    promotion_class: EnumPromotionClass = EnumPromotionClass.CLEAN_MAIN,
) -> ModelProdPromotionGateCommand:
    return ModelProdPromotionGateCommand(
        correlation_id=uuid4(),
        runtime_lane=EnumRuntimeLane.PROD,
        requested_image_digest=_DIGEST_STABILITY,
        promotion_batch_id=_BATCH,
        readiness_projection=projection,
        occ_gate_state=EnumOccGateState.MERGED,
        rollback_target=_ROLLBACK,
        requested_by=_REQUESTER,
        promotion_grant=_grant(),
        promotion_class=promotion_class,
        non_main_lineage=False,
        evaluated_at=_EVALUATED_AT,
    )


async def _dispatch(payload: object) -> ModelProdPromotionGateDecision:
    """Drive the real runtime adapter and await the def-B coroutine result."""
    result = _invoke_handle_method(HandlerProdPromotionGate().handle, payload)
    assert inspect.isawaitable(result), "def-B handle must be async (awaitable)"
    decision = await cast(Awaitable[object], result)
    assert isinstance(decision, ModelProdPromotionGateDecision)
    return decision


@pytest.mark.asyncio
async def test_defb_dispatch_dict_payload_allow() -> None:
    """operation_match dict payload -> adapter coerces to input_model -> ALLOW."""
    command = _command(projection=_ready())
    decision = await _dispatch(command.model_dump(mode="json"))
    assert decision.allowed is True
    assert decision.image_digest == _DIGEST_STABILITY


@pytest.mark.asyncio
async def test_defb_dispatch_dict_payload_block_candidate() -> None:
    """Dict payload for a stability-candidate image -> refused for prod."""
    command = _command(
        projection=_ready(),
        promotion_class=EnumPromotionClass.STABILITY_CANDIDATE,
    )
    decision = await _dispatch(command.model_dump(mode="json"))
    assert decision.allowed is False
    assert EnumProdGrantReason.CANDIDATE_NOT_AUTHORIZED.value in decision.reason
    assert decision.image_digest is None


@pytest.mark.asyncio
async def test_defb_dispatch_model_payload_matches_dict() -> None:
    """The concrete-model dispatch path equals the dict-coercion path (equivalence)."""
    command = _command(projection=_ready())
    from_model = await _dispatch(command)
    from_dict = await _dispatch(command.model_dump(mode="json"))
    assert from_model == from_dict
    assert from_model.allowed is True


@pytest.mark.asyncio
async def test_defb_handle_returns_decision_directly() -> None:
    """def-B handle returns the decision itself — not a ModelHandlerOutput wrapper."""
    command = _command(projection=None)  # no readiness -> fails closed
    decision = await HandlerProdPromotionGate().handle(command)
    assert isinstance(decision, ModelProdPromotionGateDecision)
    assert decision.allowed is False


def test_handle_is_canonical_defb() -> None:
    """Lock the canonical def-B shape: one positional model param, no envelope type."""
    signature = inspect.signature(HandlerProdPromotionGate.handle, eval_str=True)
    positional = [
        param
        for name, param in signature.parameters.items()
        if name != "self"
        and param.kind
        in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
    ]
    assert len(positional) == 1, "def-B handle must have exactly one positional param"
    annotation = positional[0].annotation
    assert isinstance(annotation, type)
    assert issubclass(annotation, BaseModel)
    assert annotation is ModelProdPromotionGateCommand
    return_annotation = signature.return_annotation
    assert return_annotation is ModelProdPromotionGateDecision

    module_text = Path(
        cast(Any, inspect.getsourcefile(HandlerProdPromotionGate))
    ).read_text(encoding="utf-8")
    assert "ModelEventEnvelope" not in module_text, "C-core: no envelope type in core"
    assert "ModelHandlerOutput" not in module_text, "def-B core returns the decision"
