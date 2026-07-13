# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Cross-boundary seam test for OMN-14532.

Drives node_generation_consumer's REAL _emit_registration wire payload (via
HandlerGenerationConsumer.handle(), no mocking of the payload shape) through
node_projection_mcp_tools' actual ModelMcpToolRegistrationEvent + project() —
producer and consumer, not two independent unit suites.

RED (documented, not re-asserted here — see git history / PR description):
before OMN-14532, the payload published by _emit_registration carried no
`contract_metadata` key at all, so `meta.get("description", ...)` and
`meta.get("modelId", meta.get("model_id", ...))` always fell through to "".
Every generation-sourced mcp_tools row had description="" and model_id=""
forever, regardless of how many tools were generated.

GREEN: this test proves the real payload now carries contract_metadata and
that projecting it produces a non-empty description/model_id row.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_generation_consumer.models.model_generation import (
    ModelNodeGenerationRequest,
)
from omnimarket.nodes.node_projection_mcp_tools.handlers.handler_projection_mcp_tools import (
    HandlerProjectionMcpTools,
    ModelMcpToolRegistrationEvent,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
from tests.unit.nodes.node_generation_consumer.test_handler_generation_consumer import (
    _VALID_LLM_RESPONSE,
    _make_handler,
    _registration_event,
)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_generation_consumer_registration_payload_projects_real_description_and_model_id() -> (
    None
):
    published: list[tuple[str, bytes]] = []
    handler = _make_handler([_VALID_LLM_RESPONSE], published=published)

    await handler.handle(
        ModelNodeGenerationRequest(
            task_description="Cross-boundary seam proof for OMN-14532",
            correlation_id="corr-omn14532-seam",
        )
    )

    _, real_payload = _registration_event(published)

    # This is the ACTUAL dict node_generation_consumer put on the wire —
    # not a hand-rolled stand-in. It carries `_topic`/no such marker here
    # since this is the raw producer payload prior to any transport
    # envelope wrapping.
    event = ModelMcpToolRegistrationEvent(**real_payload)
    assert event.mcp_eligible is True  # tags-derivation (OMN-14005) still holds

    db = InmemoryDatabaseAdapter()
    result = HandlerProjectionMcpTools().project(event, db)

    assert result.rows_upserted == 1
    row = db.query("mcp_tools")[0]
    assert row["description"] == "Cross-boundary seam proof for OMN-14532"
    assert row["model_id"] != ""
