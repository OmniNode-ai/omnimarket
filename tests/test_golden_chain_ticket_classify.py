# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain test for node_ticket_classify_compute.

Exercises both classification paths:
1. Contract-driven (seam boundaries present)
2. Keyword fallback (no seam boundaries)
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from omnimarket.enums.enum_buildability import EnumBuildability
from omnimarket.nodes.node_ticket_classify_compute.handlers.handler_ticket_classify import (
    HandlerTicketClassify,
)
from omnimarket.nodes.node_ticket_classify_compute.models.model_seam_boundaries import (
    ModelConsumedProtocol,
    ModelSeamBoundaries,
)
from omnimarket.nodes.node_ticket_classify_compute.models.model_ticket_for_classification import (
    ModelTicketForClassification,
)


@pytest.fixture
def handler() -> HandlerTicketClassify:
    return HandlerTicketClassify()


class TestGoldenChainTicketClassify:
    """Golden chain: handler classifies a batch of mixed tickets correctly."""

    @pytest.mark.asyncio
    async def test_mixed_batch_classification(
        self, handler: HandlerTicketClassify
    ) -> None:
        """Classify a batch with contract-driven, keyword, and skip tickets."""
        tickets = (
            # Contract-driven: mockable seam -> AUTO_BUILDABLE
            ModelTicketForClassification(
                ticket_id="OMN-5113",
                title="Wire auth handler",
                description="Depends on auth service (blocked by OMN-5100)",
                seam_boundaries=ModelSeamBoundaries(
                    consumes=(
                        ModelConsumedProtocol(
                            protocol="ProtocolAuth",
                            module="omnibase_spi.protocols.protocol_auth",
                            mock_available=True,
                        ),
                    ),
                ),
            ),
            # Contract-driven: unmockable seam -> BLOCKED
            ModelTicketForClassification(
                ticket_id="OMN-5120",
                title="Add new infra",
                seam_boundaries=ModelSeamBoundaries(
                    consumes=(
                        ModelConsumedProtocol(
                            protocol="ProtocolNewThing",
                            module="not.yet.exists",
                            mock_available=False,
                        ),
                    ),
                ),
            ),
            # Keyword fallback: buildable title -> AUTO_BUILDABLE
            ModelTicketForClassification(
                ticket_id="OMN-5130",
                title="Implement user profile handler",
            ),
            # Keyword fallback: terminal state -> SKIP
            ModelTicketForClassification(
                ticket_id="OMN-5140",
                title="Old completed task",
                state="Done",
            ),
            # Contract-driven via YAML string
            ModelTicketForClassification(
                ticket_id="OMN-5150",
                title="This depends on blocked team",
                contract_yaml="phase: intake\nseam_boundaries:\n  consumes:\n    - protocol: ProtocolX\n      module: x.y\n      mock_available: true\n",
            ),
        )

        cid = uuid4()
        result = await handler.handle(correlation_id=cid, tickets=tickets)

        assert len(result.classifications) == 5
        assert result.correlation_id == cid

        by_id = {c.ticket_id: c for c in result.classifications}

        assert by_id["OMN-5113"].buildability == EnumBuildability.AUTO_BUILDABLE
        assert "contract" in by_id["OMN-5113"].reason.lower()
        assert by_id["OMN-5113"].seam_source == "contract"

        assert by_id["OMN-5120"].buildability == EnumBuildability.BLOCKED
        assert "unmockable" in by_id["OMN-5120"].reason.lower()
        assert by_id["OMN-5120"].seam_source == "contract"

        assert by_id["OMN-5130"].buildability == EnumBuildability.AUTO_BUILDABLE
        assert "keyword" in by_id["OMN-5130"].reason.lower()
        assert by_id["OMN-5130"].seam_source == "keyword_fallback"

        assert by_id["OMN-5140"].buildability == EnumBuildability.SKIP

        assert by_id["OMN-5150"].buildability == EnumBuildability.AUTO_BUILDABLE
        assert "contract" in by_id["OMN-5150"].reason.lower()
        assert by_id["OMN-5150"].seam_source == "contract"

        # Counts
        assert result.total_auto_buildable == 3
        assert result.total_non_buildable == 2
