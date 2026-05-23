# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Node-local coverage for handler_test_generator dep-health discovery."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from omnibase_core.models.ticket.model_ticket_contract import ModelTicketContract

from omnimarket.nodes.node_test_generator.handlers.handler_test_generator import (
    HandlerTestGenerator,
)
from omnimarket.nodes.node_test_generator.models.model_test_generation_request import (
    ModelTestGenerationRequest,
)


@pytest.mark.unit
def test_handler_test_generator_produces_deterministic_source() -> None:
    contract = ModelTicketContract(
        ticket_id="OMN-11677",
        title="Node test generator coverage",
        context={
            "node_type": "compute",
            "input_model": "ModelFooRequest",
            "output_model": "ModelFooResult",
        },
        created_at=datetime(2026, 5, 22, 0, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 5, 22, 0, 0, 0, tzinfo=UTC),
    )
    request = ModelTestGenerationRequest(contract=contract)
    handler = HandlerTestGenerator()

    first = handler.handle(request)
    second = handler.handle(request)

    assert first.test_hash == second.test_hash
    assert "ModelFooRequest" in first.test_source
    assert "ModelFooResult" in first.test_source
