# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain test for node_ticket_plan_compute — zero infra."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml


@pytest.fixture
def node_dir() -> Path:
    return Path(__file__).resolve().parent.parent


@pytest.fixture
def contract_path(node_dir: Path) -> Path:
    return node_dir / "contract.yaml"


@pytest.fixture
def metadata_path(node_dir: Path) -> Path:
    return node_dir / "metadata.yaml"


class TestContractYaml:
    def test_contract_exists(self, contract_path: Path) -> None:
        assert contract_path.exists()

    def test_contract_loads(self, contract_path: Path) -> None:
        with open(contract_path) as f:
            data = yaml.safe_load(f)
        assert isinstance(data, dict)
        assert "name" in data
        assert "handler" in data

    def test_contract_declares_handler(self, contract_path: Path) -> None:
        with open(contract_path) as f:
            data = yaml.safe_load(f)
        handler = data.get("handler", {})
        assert "module" in handler
        assert "class" in handler


class TestMetadataYaml:
    def test_metadata_exists(self, metadata_path: Path) -> None:
        assert metadata_path.exists()

    def test_metadata_loads(self, metadata_path: Path) -> None:
        with open(metadata_path) as f:
            data = yaml.safe_load(f)
        assert isinstance(data, dict)
        assert "name" in data
        assert "version" in data
        assert "entry_points" in data


class TestHandlerImport:
    def test_handler_module_imports(self) -> None:
        from omnimarket.nodes.node_ticket_plan_compute.handlers import (  # noqa: F401
            handler_ticket_plan_compute,
        )

    def test_handler_class_exists(self) -> None:
        from omnimarket.nodes.node_ticket_plan_compute.handlers.handler_ticket_plan_compute import (
            HandlerTicketPlanCompute,
        )

        assert HandlerTicketPlanCompute is not None

    def test_input_model_exists(self) -> None:
        from omnimarket.nodes.node_ticket_plan_compute.models.model_ticket_plan_request import (
            ModelTicketPlanRequest,
        )

        assert ModelTicketPlanRequest is not None

    def test_output_model_exists(self) -> None:
        from omnimarket.nodes.node_ticket_plan_compute.models.model_ticket_plan_result import (
            ModelTicketPlanResult,
        )

        assert ModelTicketPlanResult is not None


class TestHandlerBehavior:
    def test_handler_parses_markdown_bullets(self) -> None:
        from omnimarket.nodes.node_ticket_plan_compute.handlers.handler_ticket_plan_compute import (
            HandlerTicketPlanCompute,
        )
        from omnimarket.nodes.node_ticket_plan_compute.models.model_ticket_plan_request import (
            ModelTicketPlanRequest,
        )

        handler = HandlerTicketPlanCompute()
        request = ModelTicketPlanRequest(
            plan_text=(
                "## Phase 1\n"
                "- [ ] Ticket A: Do alpha work; labels: backend, linear\n"
                "- Ticket B — Do beta work; depends on Ticket A\n"
            )
        )

        result = handler.handle(request)

        assert [ticket.title for ticket in result.tickets] == ["Ticket A", "Ticket B"]
        assert result.tickets[0].phase == "Phase 1"
        assert result.tickets[0].labels == ["backend", "linear"]
        assert result.tickets[1].depends_on == ["Ticket A"]
        assert result.parse_warnings == []
