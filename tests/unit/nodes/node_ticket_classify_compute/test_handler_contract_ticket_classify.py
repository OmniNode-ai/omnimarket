# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for contract-driven ticket classifier."""

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


class TestContractDrivenClassification:
    """Tests for the new contract-driven path."""

    @pytest.mark.asyncio
    async def test_ticket_with_mockable_seams_is_auto_buildable(
        self, handler: HandlerTicketClassify
    ) -> None:
        ticket = ModelTicketForClassification(
            ticket_id="OMN-5113",
            title="Add dependency on blocked service",
            description="This depends on external service that is blocked",
            seam_boundaries=ModelSeamBoundaries(
                consumes=(
                    ModelConsumedProtocol(
                        protocol="ProtocolExternalService",
                        module="some.module",
                        mock_available=True,
                    ),
                ),
            ),
        )
        result = await handler.handle(correlation_id=uuid4(), tickets=(ticket,))
        assert result.classifications[0].buildability == EnumBuildability.AUTO_BUILDABLE
        assert result.classifications[0].confidence >= 0.8
        assert result.classifications[0].seam_source == "contract"

    @pytest.mark.asyncio
    async def test_ticket_with_unmockable_seams_is_blocked(
        self, handler: HandlerTicketClassify
    ) -> None:
        ticket = ModelTicketForClassification(
            ticket_id="OMN-5114",
            title="Add integration",
            seam_boundaries=ModelSeamBoundaries(
                consumes=(
                    ModelConsumedProtocol(
                        protocol="ProtocolNewInfra",
                        module="not.yet.exists",
                        mock_available=False,
                    ),
                ),
            ),
        )
        result = await handler.handle(correlation_id=uuid4(), tickets=(ticket,))
        assert result.classifications[0].buildability == EnumBuildability.BLOCKED
        assert result.classifications[0].seam_source == "contract"

    @pytest.mark.asyncio
    async def test_ticket_with_empty_seams_is_auto_buildable(
        self, handler: HandlerTicketClassify
    ) -> None:
        ticket = ModelTicketForClassification(
            ticket_id="OMN-5115",
            title="Pure refactor",
            seam_boundaries=ModelSeamBoundaries(),
        )
        result = await handler.handle(correlation_id=uuid4(), tickets=(ticket,))
        assert result.classifications[0].buildability == EnumBuildability.AUTO_BUILDABLE
        assert result.classifications[0].confidence == 0.7
        assert result.classifications[0].seam_source == "contract"

    @pytest.mark.asyncio
    async def test_contract_yaml_parsed_for_seams(
        self, handler: HandlerTicketClassify
    ) -> None:
        """When contract_yaml has seam_boundaries, they are parsed."""
        yaml_str = """phase: intake
seam_boundaries:
  consumes:
    - protocol: ProtocolFoo
      module: foo.bar
      mock_available: true
  produces:
    - protocol: ProtocolBar
      module: bar.baz
"""
        ticket = ModelTicketForClassification(
            ticket_id="OMN-5116",
            title="This depends on blocked external team",
            contract_yaml=yaml_str,
        )
        result = await handler.handle(correlation_id=uuid4(), tickets=(ticket,))
        assert result.classifications[0].buildability == EnumBuildability.AUTO_BUILDABLE
        assert result.classifications[0].seam_source == "contract"


class TestKeywordFallback:
    """Tests for the keyword fallback path (no seam_boundaries)."""

    @pytest.mark.asyncio
    async def test_buildable_keywords_in_title(
        self, handler: HandlerTicketClassify
    ) -> None:
        ticket = ModelTicketForClassification(
            ticket_id="OMN-5200",
            title="Implement user auth handler",
        )
        result = await handler.handle(correlation_id=uuid4(), tickets=(ticket,))
        assert result.classifications[0].buildability == EnumBuildability.AUTO_BUILDABLE
        assert result.classifications[0].confidence <= 0.6
        assert result.classifications[0].seam_source == "keyword_fallback"

    @pytest.mark.asyncio
    async def test_blocked_keywords_trigger_blocked(
        self, handler: HandlerTicketClassify
    ) -> None:
        ticket = ModelTicketForClassification(
            ticket_id="OMN-5201",
            title="Integrate with vendor API",
            description="Blocked waiting on vendor credentials",
        )
        result = await handler.handle(correlation_id=uuid4(), tickets=(ticket,))
        assert result.classifications[0].buildability == EnumBuildability.BLOCKED
        assert result.classifications[0].confidence <= 0.6

    @pytest.mark.asyncio
    async def test_skip_keywords(self, handler: HandlerTicketClassify) -> None:
        ticket = ModelTicketForClassification(
            ticket_id="OMN-5202",
            title="Old task",
            state="Done",
        )
        result = await handler.handle(correlation_id=uuid4(), tickets=(ticket,))
        assert result.classifications[0].buildability == EnumBuildability.SKIP


class TestContractOverridesKeywords:
    """The critical test: contract seams override keyword heuristics."""

    @pytest.mark.asyncio
    async def test_depends_on_keyword_overridden_by_mockable_seam(
        self, handler: HandlerTicketClassify
    ) -> None:
        """A ticket with 'depends on' in description but mockable seams is buildable."""
        ticket = ModelTicketForClassification(
            ticket_id="OMN-5300",
            title="Wire auth middleware",
            description="Depends on OMN-299 for the auth protocol definition",
            seam_boundaries=ModelSeamBoundaries(
                consumes=(
                    ModelConsumedProtocol(
                        protocol="ProtocolAuth",
                        module="omnibase_spi.protocols.protocol_auth",
                        mock_available=True,
                    ),
                ),
            ),
        )
        result = await handler.handle(correlation_id=uuid4(), tickets=(ticket,))
        assert result.classifications[0].buildability == EnumBuildability.AUTO_BUILDABLE
        assert "contract" in result.classifications[0].reason.lower()
        assert result.classifications[0].seam_source == "contract"


class TestClassificationTruthBoundary:
    """Tests that validate the classification truth boundary.

    The truth boundary is: contract-driven classification is authoritative.
    Keyword fallback is a heuristic that can be wrong. The seam_source field
    makes this explicit so downstream consumers can distinguish the two.
    """

    @pytest.mark.asyncio
    async def test_contract_path_high_confidence(
        self, handler: HandlerTicketClassify
    ) -> None:
        """Contract path with non-empty seams should have confidence >= 0.85."""
        ticket = ModelTicketForClassification(
            ticket_id="OMN-5400",
            title="Test truth boundary",
            seam_boundaries=ModelSeamBoundaries(
                consumes=(
                    ModelConsumedProtocol(
                        protocol="ProtocolFoo",
                        module="foo.bar",
                        mock_available=True,
                    ),
                ),
            ),
        )
        result = await handler.handle(correlation_id=uuid4(), tickets=(ticket,))
        c = result.classifications[0]
        assert c.confidence >= 0.85
        assert c.seam_source == "contract"

    @pytest.mark.asyncio
    async def test_keyword_path_capped_confidence(
        self, handler: HandlerTicketClassify
    ) -> None:
        """Keyword fallback confidence must be <= 0.6."""
        ticket = ModelTicketForClassification(
            ticket_id="OMN-5401",
            title="Implement create add fix update refactor wire handler model",
        )
        result = await handler.handle(correlation_id=uuid4(), tickets=(ticket,))
        c = result.classifications[0]
        assert c.confidence <= 0.6
        assert c.seam_source == "keyword_fallback"

    @pytest.mark.asyncio
    async def test_empty_seams_lower_confidence(
        self, handler: HandlerTicketClassify
    ) -> None:
        """Empty seams get 0.7 confidence, not 0.9."""
        ticket = ModelTicketForClassification(
            ticket_id="OMN-5402",
            title="Pure refactor with empty seams",
            seam_boundaries=ModelSeamBoundaries(),
        )
        result = await handler.handle(correlation_id=uuid4(), tickets=(ticket,))
        c = result.classifications[0]
        assert c.confidence == 0.7
        assert c.seam_source == "contract"
