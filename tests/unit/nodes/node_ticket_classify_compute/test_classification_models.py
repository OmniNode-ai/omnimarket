# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for ticket classification models."""

from __future__ import annotations

from uuid import uuid4

from omnimarket.enums.enum_buildability import EnumBuildability
from omnimarket.nodes.node_ticket_classify_compute.models.model_ticket_classification import (
    ModelTicketClassification,
)
from omnimarket.nodes.node_ticket_classify_compute.models.model_ticket_classify_output import (
    ModelTicketClassifyOutput,
)
from omnimarket.nodes.node_ticket_classify_compute.models.model_ticket_for_classification import (
    ModelTicketForClassification,
)


class TestModelTicketForClassification:
    def test_minimal(self) -> None:
        t = ModelTicketForClassification(ticket_id="OMN-1234", title="Fix bug")
        assert t.contract_yaml is None
        assert t.seam_boundaries is None

    def test_with_contract_yaml(self) -> None:
        yaml_str = "phase: intake\nseam_boundaries:\n  consumes: []"
        t = ModelTicketForClassification(
            ticket_id="OMN-1234",
            title="Fix bug",
            contract_yaml=yaml_str,
        )
        assert t.contract_yaml == yaml_str


class TestModelTicketClassifyOutput:
    def test_round_trip(self) -> None:
        cid = uuid4()
        classification = ModelTicketClassification(
            ticket_id="OMN-1234",
            title="Fix bug",
            buildability=EnumBuildability.AUTO_BUILDABLE,
            confidence=0.9,
            reason="Contract seams declared and mockable",
            seam_source="contract",
        )
        output = ModelTicketClassifyOutput(
            correlation_id=cid,
            classifications=(classification,),
            total_auto_buildable=1,
            total_non_buildable=0,
        )
        assert output.total_auto_buildable == 1


class TestModelTicketClassification:
    def test_seam_source_field(self) -> None:
        c = ModelTicketClassification(
            ticket_id="OMN-5555",
            title="Test seam source",
            buildability=EnumBuildability.AUTO_BUILDABLE,
            confidence=0.9,
            reason="test",
            seam_source="contract",
        )
        assert c.seam_source == "contract"

    def test_seam_source_default(self) -> None:
        c = ModelTicketClassification(
            ticket_id="OMN-5555",
            title="Test default",
            buildability=EnumBuildability.BLOCKED,
            confidence=0.5,
            reason="test",
        )
        assert c.seam_source == "keyword_fallback"
