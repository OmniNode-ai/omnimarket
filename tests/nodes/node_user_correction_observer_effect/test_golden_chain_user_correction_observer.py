# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Golden-chain test for node_user_correction_observer_effect (OMN-12846).

Drives the observer handler end-to-end: a typed user-correction command envelope
in -> a durable ModelUserCorrectionEvent fact out, published on the
contract-declared topic. No bus/Kafka I/O: the handler returns the emitted event
as an EFFECT output, exactly as the runtime routes it.
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
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

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
    correlation_id = uuid4()
    inbound: ModelEventEnvelope[ModelUserCorrectionEvent] = ModelEventEnvelope(
        payload=correction,
        correlation_id=correlation_id,
        event_type="onex.cmd.omnimarket.user-correction-observed.v1",
    )

    handler = HandlerUserCorrectionObserver()
    output = await handler.handle(inbound)

    assert output.node_kind is EnumNodeKind.EFFECT
    assert len(output.events) == 1

    emitted = output.events[0]
    contract = yaml.safe_load(_CONTRACT_PATH.read_text())
    expected_topic = contract["event_bus"]["publish_topics"][0]
    assert emitted.event_type == expected_topic
    assert emitted.correlation_id == correlation_id

    fact = emitted.payload
    assert isinstance(fact, ModelUserCorrectionEvent)
    assert fact.category is EnumUserCorrectionCategory.CONSTRAINT_VIOLATION
    assert fact.failure_axis is EnumCorrectionFailureAxis.MISUNDERSTANDING
    assert fact.counts_toward_context_failure is True


@pytest.mark.unit
async def test_golden_chain_accepts_dict_payload() -> None:
    """A dict payload is validated into the typed correction event."""
    correction = _make_correction(
        failure_axis=EnumCorrectionFailureAxis.NEW_INFORMATION
    )
    inbound: ModelEventEnvelope[dict[str, object]] = ModelEventEnvelope(
        payload=correction.model_dump(mode="json"),
        correlation_id=uuid4(),
        event_type="onex.cmd.omnimarket.user-correction-observed.v1",
    )

    handler = HandlerUserCorrectionObserver()
    output = await handler.handle(inbound)

    emitted = output.events[0]
    fact = emitted.payload
    assert isinstance(fact, ModelUserCorrectionEvent)
    assert fact.failure_axis is EnumCorrectionFailureAxis.NEW_INFORMATION
    assert fact.counts_toward_context_failure is False
