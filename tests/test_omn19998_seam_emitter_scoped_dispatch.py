# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam-emitter dispatch scoping: an un-scoped emit_seam_count entry is a bug (OMN-19998).

``node_slack_publish_effect`` (and three sibling nodes) declare an
``emit_seam_count`` routing entry whose request model sits under the key
``input_model``. The auto-wiring discovery reads ``event_model``, so that entry
comes up with ``event_model is None``: un-scoped, it owns the contract's only
subscribe topic and therefore receives every slack-publish command. The emitter
then fails with pydantic validation errors on ``ModelSeamEmitterRequest`` even
though the command is a ``ModelSlackPublish``.

These two tests pin the defect so the later fix (giving the entry an
``event_model``) is what turns them green. The first reads the contract from
disk; the second drives a real ``ModelSlackPublish`` through the runtime's own
dispatch engine and asserts the emitter's validation error never leaks into the
result.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

from omnimarket.nodes.node_slack_publish_effect.models.model_slack_publish import (
    ModelSlackPublish,
)

pytestmark = pytest.mark.unit

NODES = (
    "node_slack_publish_effect",
    "node_factory_floor_publish_effect",
    "node_seam_conservation_compute",
    "node_factory_floor_effect",
)

CONTRACT_ROOT = Path(__file__).parents[1] / "src" / "omnimarket" / "nodes"


@pytest.mark.parametrize("node", NODES)
def test_every_emit_seam_count_entry_is_scoped_to_its_request_model(
    node: str,
) -> None:
    """The emit_seam_count entry must carry its request model as event_model.

    Discovery reads ``event_model``; an entry that only declares ``input_model``
    comes up un-scoped and swallows every command on the contract's topic.
    """
    contract_path = CONTRACT_ROOT / node / "contract.yaml"
    manifest = discover_contracts_from_paths([contract_path])
    assert not manifest.errors, manifest.errors
    contract = next(c for c in manifest.contracts if c.name == node)
    entry = next(
        h for h in contract.handler_routing.handlers if h.operation == "emit_seam_count"
    )
    assert entry.event_model is not None, (
        f"{node}: the emit_seam_count entry has no event_model; it is un-scoped "
        "and will receive every command on the contract's subscribe topic"
    )
    assert entry.event_model.name == "ModelSeamEmitterRequest"
    assert entry.event_model.module == (
        "omnimarket.models.seam_reading.model_seam_emitter"
    )


async def test_a_slack_publish_command_reaches_only_the_slack_handler() -> None:
    """A ModelSlackPublish command must not trip the seam emitter's validation.

    The envelope carries the topic-derived event_type, as a bus publisher's does:
    without it no dispatcher matches and the test would pass for the wrong reason.
    The Slack handler refuses for lack of a secret in this test; that is fine. The
    failing control is the emitter's dispatcher showing up in the result's error,
    which happens only when the un-scoped entry receives the command.
    """
    contract_path = CONTRACT_ROOT / "node_slack_publish_effect" / "contract.yaml"
    manifest = discover_contracts_from_paths([contract_path])
    assert not manifest.errors, manifest.errors
    contract = next(
        c for c in manifest.contracts if c.name == "node_slack_publish_effect"
    )
    topic = contract.event_bus.subscribe_topics[0]

    bus = EventBusInmemory(environment="seam-scoped", group="seam-scoped")
    await bus.start()
    engine = MessageDispatchEngine()
    report = await wire_from_manifest(
        ModelAutoWiringManifest(contracts=tuple(manifest.contracts)),
        engine,
        event_bus=bus,
        environment="local",
    )
    assert report.total_failed == 0
    engine.freeze()

    request = ModelSlackPublish(
        channel="C0TEST",
        text="hello",
        idempotency_key="test-key",
        correlation_id=uuid4(),
    )
    envelope: ModelEventEnvelope[object] = ModelEventEnvelope(
        payload=request.model_dump(mode="json"),
        correlation_id=request.correlation_id,
        event_type="omnimarket.slack-publish",
    )
    try:
        result = await engine.dispatch(topic, envelope)
    finally:
        await bus.close()
    error_message = result.error_message or ""
    assert "HandlerSeamEmitter" not in error_message, (
        "the un-scoped emit_seam_count entry received the slack-publish command: "
        f"{error_message}"
    )
    assert "ModelSeamEmitterRequest" not in error_message
