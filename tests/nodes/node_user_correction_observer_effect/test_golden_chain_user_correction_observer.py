# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Golden-chain test for node_user_correction_observer_effect (OMN-12846).

Drives the observer handler end-to-end: the typed user-correction request the
runtime adapter unwraps from the command event in -> a durable
ModelUserCorrectionEvent fact out, which the runtime publishes on the one
contract-declared publish topic. No bus/Kafka I/O: the handler returns the typed
fact as an EFFECT output.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_core.enums.enum_correction_failure_axis import EnumCorrectionFailureAxis
from omnibase_core.enums.enum_node_kind import EnumNodeKind
from omnibase_core.enums.enum_user_correction_category import EnumUserCorrectionCategory

from omnimarket.intelligence.events import ModelUserCorrectionEvent
from omnimarket.nodes.node_user_correction_observer_effect.handler_user_correction_observer import (
    HandlerUserCorrectionObserver,
)

_VALID_HASH = "sha256:" + "c" * 64
_VALID_FACTOR_HASH = "sha256:" + "d" * 64

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_user_correction_observer_effect"
    / "contract.yaml"
)


def _make_correction(
    *,
    failure_axis: EnumCorrectionFailureAxis = (
        EnumCorrectionFailureAxis.MISUNDERSTANDING
    ),
) -> ModelUserCorrectionEvent:
    return ModelUserCorrectionEvent(
        session_id="sess-golden",
        correlation_id=uuid4(),
        category=EnumUserCorrectionCategory.CONSTRAINT_VIOLATION,
        failure_axis=failure_axis,
        context_pack_hash=_VALID_HASH,
        factor_subset_hash=_VALID_FACTOR_HASH,
        emitted_at=datetime.now(UTC),
    )


@pytest.mark.unit
async def test_golden_chain_republishes_correction_on_contract_topic() -> None:
    """A correction command yields one EFFECT event on the contract publish topic."""
    correction = _make_correction()

    handler = HandlerUserCorrectionObserver()
    output = await handler.handle(correction)

    assert output.node_kind is EnumNodeKind.EFFECT
    assert len(output.events) == 1
    assert output.correlation_id == correction.correlation_id

    contract = yaml.safe_load(_CONTRACT_PATH.read_text())
    assert contract["event_bus"]["publish_topics"] == [handler.publish_topic]

    fact = output.events[0]
    assert isinstance(fact, ModelUserCorrectionEvent)
    assert fact.category is EnumUserCorrectionCategory.CONSTRAINT_VIOLATION
    assert fact.failure_axis is EnumCorrectionFailureAxis.MISUNDERSTANDING
    assert fact.counts_toward_context_failure is True


@pytest.mark.unit
async def test_golden_chain_takes_the_request_validated_from_a_dict() -> None:
    """A dict payload validates into the typed request the handler takes."""
    correction = _make_correction(
        failure_axis=EnumCorrectionFailureAxis.NEW_INFORMATION
    )
    request = ModelUserCorrectionEvent.model_validate(
        correction.model_dump(mode="json")
    )

    handler = HandlerUserCorrectionObserver()
    output = await handler.handle(request)

    fact = output.events[0]
    assert isinstance(fact, ModelUserCorrectionEvent)
    assert fact.failure_axis is EnumCorrectionFailureAxis.NEW_INFORMATION
    assert fact.counts_toward_context_failure is False
