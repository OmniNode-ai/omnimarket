# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for node_decompose_epic_orchestrator [OMN-12214].

Verifies:
- Models instantiate correctly with frozen config
- Handler stub raises NotImplementedError
- ModelDecomposeEpicResult.count property works
- Contract YAML is loadable and contains required fields
"""

from __future__ import annotations

import uuid

import pytest
import yaml

from omnimarket.nodes.node_decompose_epic_orchestrator.handlers.handler_decompose_epic import (
    HandlerDecomposeEpicOrchestrator,
)
from omnimarket.nodes.node_decompose_epic_orchestrator.models.model_decompose_epic_request import (
    ModelCreatedSubTicket,
    ModelDecomposeEpicRequest,
    ModelDecomposeEpicResult,
)

_CORR_ID = uuid.UUID("00000000-0000-4000-a000-000000000099")


# ---------------------------------------------------------------------------
# Model construction
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_model_decompose_epic_request_defaults() -> None:
    req = ModelDecomposeEpicRequest(
        epic_id="OMN-2000",
        correlation_id=_CORR_ID,
    )
    assert req.epic_id == "OMN-2000"
    assert req.max_tickets == 10
    assert req.generate_contracts is True
    assert req.dry_run is False
    assert req.correlation_id == _CORR_ID


@pytest.mark.unit
def test_model_decompose_epic_request_dry_run() -> None:
    req = ModelDecomposeEpicRequest(
        epic_id="OMN-2000",
        max_tickets=5,
        generate_contracts=False,
        dry_run=True,
        correlation_id=_CORR_ID,
    )
    assert req.dry_run is True
    assert req.max_tickets == 5
    assert req.generate_contracts is False


@pytest.mark.unit
def test_model_decompose_epic_request_is_frozen() -> None:
    req = ModelDecomposeEpicRequest(
        epic_id="OMN-2000",
        correlation_id=_CORR_ID,
    )
    with pytest.raises((TypeError, ValueError)):
        req.epic_id = "OMN-9999"  # type: ignore[misc]


@pytest.mark.unit
def test_model_decompose_epic_request_max_tickets_bounds() -> None:
    with pytest.raises((TypeError, ValueError)):
        ModelDecomposeEpicRequest(
            epic_id="OMN-2000",
            max_tickets=0,
            correlation_id=_CORR_ID,
        )
    with pytest.raises((TypeError, ValueError)):
        ModelDecomposeEpicRequest(
            epic_id="OMN-2000",
            max_tickets=51,
            correlation_id=_CORR_ID,
        )


@pytest.mark.unit
def test_model_created_sub_ticket() -> None:
    ticket = ModelCreatedSubTicket(
        ticket_id="OMN-2001",
        title="Implement X",
        repo_hint="omniclaude",
        linear_id="abc-123",
    )
    assert ticket.ticket_id == "OMN-2001"
    assert ticket.repo_hint == "omniclaude"


@pytest.mark.unit
def test_model_decompose_epic_result_defaults() -> None:
    result = ModelDecomposeEpicResult(
        epic_id="OMN-2000",
        status="dry_run",
        correlation_id=_CORR_ID,
    )
    assert result.count == 0
    assert result.created_tickets == ()
    assert result.contract_files_generated == ()
    assert result.contract_pr_url is None


@pytest.mark.unit
def test_model_decompose_epic_result_count() -> None:
    tickets = (
        ModelCreatedSubTicket(
            ticket_id="OMN-2001",
            title="Implement X",
            repo_hint="omniclaude",
            linear_id="abc-1",
        ),
        ModelCreatedSubTicket(
            ticket_id="OMN-2002",
            title="Add node Y",
            repo_hint="omnibase_core",
            linear_id="abc-2",
        ),
    )
    result = ModelDecomposeEpicResult(
        epic_id="OMN-2000",
        status="success",
        created_tickets=tickets,
        correlation_id=_CORR_ID,
    )
    assert result.count == 2


# ---------------------------------------------------------------------------
# Handler stub
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handler_raises_not_implemented() -> None:
    handler = HandlerDecomposeEpicOrchestrator()
    req = ModelDecomposeEpicRequest(
        epic_id="OMN-2000",
        correlation_id=_CORR_ID,
    )
    with pytest.raises(NotImplementedError, match="stub"):
        await handler.handle(req)


# ---------------------------------------------------------------------------
# Contract YAML
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_contract_yaml_loads_and_has_required_fields() -> None:
    import pathlib

    contract_path = (
        pathlib.Path(__file__).parent.parent
        / "src/omnimarket/nodes/node_decompose_epic_orchestrator/contract.yaml"
    )
    assert contract_path.exists(), f"contract.yaml not found at {contract_path}"
    with contract_path.open() as f:
        data = yaml.safe_load(f)

    assert data["name"] == "node_decompose_epic_orchestrator"
    assert data["node_type"] == "orchestrator"
    assert data["node_not_implemented"] is True
    assert "event_bus" in data
    assert (
        "onex.cmd.omnimarket.decompose-epic.v1" in data["event_bus"]["subscribe_topics"]
    )
    assert (
        "onex.evt.omnimarket.epic-decomposed.v1" in data["event_bus"]["publish_topics"]
    )
