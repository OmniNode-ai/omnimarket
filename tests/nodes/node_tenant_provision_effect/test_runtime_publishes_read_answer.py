# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The runtime's own result applier, built from this node's contract, publishes a read answer on the read-completed topic.

On dev the read command was consumed and handled, and its answer was published on the provision-completed topic (the
contract's terminal event, the fallback for a result class no ``published_events`` entry names), so a caller waiting on
``tenant-read-completed`` timed out. This drives the applier the runtime builds, with a capturing bus, for each result class.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from omnibase_infra.event_bus.models.model_publish_receipt import ModelPublishReceipt
from omnibase_infra.models.dispatch.model_dispatch_result import ModelDispatchResult
from omnibase_infra.runtime.event_bus_subcontract_wiring import (
    load_published_events_map,
)
from omnibase_infra.runtime.service_dispatch_result_applier import (
    build_contract_result_applier,
)

from omnimarket.handlers.libyaml_loader import load_yaml_text
from omnimarket.nodes.node_tenant_provision_effect.models.model_tenant_provision import (
    EnumTenantReadOutcome,
    ModelTenantReadResult,
)

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_tenant_provision_effect/contract.yaml"
)
READ_COMPLETED = "onex.evt.omnimarket.tenant-read-completed.v1"


class _CapturingBus:
    def __init__(self) -> None:
        self.topics: list[str] = []

    async def publish_envelope(
        self, envelope: object, topic: str, *, key: bytes | None = None
    ) -> None:
        self.topics.append(topic)

    async def publish(
        self, topic: str, key: bytes | None, value: bytes
    ) -> ModelPublishReceipt | None:
        raise AssertionError("the runtime publishes envelopes")

    def get_consumer_groups(self) -> dict[tuple[str, str], str]:
        return {}


async def _topics_for_read_answer(contract_path: Path) -> list[str]:
    contract: dict[str, Any] = load_yaml_text(contract_path.read_text(encoding="utf-8"))
    bus = _CapturingBus()
    publish_topics = contract["event_bus"]["publish_topics"]
    applier = build_contract_result_applier(
        event_bus=bus,
        contract_path=contract_path,
        publish_topics=publish_topics,
        output_topic=contract["terminal_event"],
        output_topic_map=load_published_events_map(contract_path),
        allowed_output_topics=publish_topics,
    )
    answer = ModelTenantReadResult(
        command_id=uuid4(),
        outcome=EnumTenantReadOutcome.NOT_FOUND,
        detail="no tenant with that id or slug",
        emitted_at=datetime.now(tz=UTC),
    )
    await applier.apply(
        ModelDispatchResult(
            status="success",  # type: ignore[arg-type]
            topic="onex.cmd.omnimarket.tenant-read-requested.v1",
            started_at=datetime.now(tz=UTC),
            output_events=[answer],
        )
    )
    return bus.topics


@pytest.mark.asyncio
async def test_a_read_answer_is_published_on_the_read_completed_topic() -> None:
    assert await _topics_for_read_answer(CONTRACT) == [READ_COMPLETED]


@pytest.mark.asyncio
async def test_positive_control_the_contract_before_the_change_answered_on_the_provision_topic(
    tmp_path: Path,
) -> None:
    """The same applier over the contract as it stood (no published_events, one publish topic) misroutes the answer."""
    old = tmp_path / "contract.yaml"
    text = CONTRACT.read_text(encoding="utf-8")
    head, _, rest = text.partition("  publish_topics:\n")
    _, _, tail = rest.partition("externally_produced_topics:")
    old.write_text(
        head
        + "  publish_topics:\n    - onex.evt.omnimarket.tenant-provision-completed.v1\n"
        + "externally_produced_topics:"
        + tail,
        encoding="utf-8",
    )
    assert await _topics_for_read_answer(old) == [
        "onex.evt.omnimarket.tenant-provision-completed.v1"
    ]
