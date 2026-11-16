# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam: who consumes onex.evt.omnimarket.pr-lifecycle-inventory-completed.v1.

OMN-24121 removed the inventory topic from node_pr_lifecycle_triage_compute's
``event_bus.subscribe_topics``. The subscription was dead weight that lied: the
topic's real publisher (node_pr_lifecycle_inventory_compute) publishes
ModelPrInventoryOutput, while the triage route validates every subscribed
payload into ModelPrTriageInput (needs ``correlation_id``, needs ``prs``,
forbids extra keys), so on the in-memory bus the triage dispatcher raised
``AttributeError: 'dict' object has no attribute 'correlation_id'`` on EVERY
inventory event and never published a triage result. Nothing real consumed
through it — the orchestrator calls the triage handler in process — so the
empty list is the truth, and the failing control is the unchanged contract:
wire it and the handler is handed a raw dict and fails on the first event.

After the fix the kernel skips the triage contract outright (no declared
subscribe topics, no subscription). The second test records the reading that
cleared the other reported consumer of the topic.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.runtime.auto_wiring.discovery import (
    discover_contracts_from_paths,
)
from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
from omnibase_infra.runtime.auto_wiring.report import EnumWiringOutcome
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine
from pydantic import ValidationError

from omnimarket.nodes.node_pr_lifecycle_inventory_compute.models.model_pr_lifecycle_inventory import (
    ModelPrInventoryOutput,
    ModelPrState,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.handlers.handler_pr_lifecycle_triage import (
    HandlerPrLifecycleTriage,
)
from omnimarket.nodes.node_projection_ci_attempt_outcome.models.model_ci_attempt_outcome import (
    ModelCiAttemptOutcomeProjectionRequest,
)

pytestmark = pytest.mark.unit

INVENTORY_TOPIC = "onex.evt.omnimarket.pr-lifecycle-inventory-completed.v1"
TRIAGE_COMPLETED_TOPIC = "onex.evt.omnimarket.pr-lifecycle-triage-completed.v1"
TRIAGE_NODE = "node_pr_lifecycle_triage_compute"

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TRIAGE_CONTRACT = (
    _REPO_ROOT / "src" / "omnimarket" / "nodes" / TRIAGE_NODE / "contract.yaml"
)


def _producer_payload() -> dict[str, Any]:
    """The JSON payload the inventory node actually publishes on the topic."""
    state = ModelPrState(
        repo="OmniNode-ai/omnimarket", pr_number=1, title="t", state="open"
    )
    inventory = ModelPrInventoryOutput(
        repo="OmniNode-ai/omnimarket",
        pr_states=(state,),
        total_collected=1,
    )
    return inventory.model_dump(mode="json")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_triage_handler_is_not_handed_the_inventory_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runtime hands the triage handler nothing from the inventory topic.

    Failing control, on the unchanged (pre-OMN-24121) contract: the route
    subscribed the topic, the dispatcher fed the inventory dict to the
    ModelPrTriageInput route and the handler was called with a raw dict and
    raised AttributeError, so no triage result was ever published. With
    ``subscribe_topics: []`` the kernel skips the contract during wiring: no
    subscription, no dispatcher call, no terminal event on the triage topic.
    """
    recorded: list[object] = []

    async def record(self: HandlerPrLifecycleTriage, request: object) -> object:
        recorded.append(request)
        return request

    monkeypatch.setattr(HandlerPrLifecycleTriage, "handle", record)

    manifest = discover_contracts_from_paths([_TRIAGE_CONTRACT])
    assert manifest.total_errors == 0, manifest.errors
    assert [contract.name for contract in manifest.contracts] == [TRIAGE_NODE]

    bus = EventBusInmemory()
    await bus.start()
    try:
        engine = MessageDispatchEngine()
        report = await wire_from_manifest(
            ModelAutoWiringManifest(contracts=tuple(manifest.contracts)),
            engine,
            event_bus=bus,
            environment="local",
        )
        assert report.total_failed == 0, report
        triage_row = next(
            row for row in report.results if row.contract_name == TRIAGE_NODE
        )
        assert triage_row.outcome is EnumWiringOutcome.SKIPPED
        assert triage_row.reason == "No event_bus.subscribe_topics declared in contract"
        engine.freeze()

        envelope = ModelEventEnvelope[object](
            payload=_producer_payload(),
            correlation_id=uuid4(),
            event_type="ModelPrInventoryOutput",
        )
        await bus.publish(
            INVENTORY_TOPIC,
            None,
            envelope.model_dump_json().encode("utf-8"),
            None,
        )

        assert recorded == []
        history = await bus.get_event_history(topic=TRIAGE_COMPLETED_TOPIC)
        assert history == []
    finally:
        await bus.close()


@pytest.mark.unit
def test_ci_attempt_consumer_accepts_the_inventory_event_with_its_envelope_time() -> (
    None
):
    """The CI-attempt projection request validates the real inventory payload.

    A field-by-field comparison reports this consumer against the inventory
    event as a defect: it wants ``pull_requests`` and ``observed_at``, keys the
    producer never sends. False alarm — its before-validator maps the event's
    ``pr_states`` onto ``pull_requests`` and the envelope's ``_envelope_timestamp``
    onto ``observed_at``, which is why the envelope-stamped payload validates
    and the bare payload refuses.
    """
    payload = _producer_payload()

    stamped = {**payload, "_envelope_timestamp": "2026-11-13T12:00:00+00:00"}
    request = ModelCiAttemptOutcomeProjectionRequest.model_validate(stamped)
    assert request.observed_at == datetime(2026, 11, 13, 12, 0, 0, tzinfo=UTC)
    assert [pr.pr_number for pr in request.pull_requests] == [1]

    with pytest.raises(ValidationError, match="observed_at"):
        ModelCiAttemptOutcomeProjectionRequest.model_validate(payload)
