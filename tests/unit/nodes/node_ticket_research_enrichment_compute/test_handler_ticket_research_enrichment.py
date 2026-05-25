# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for HandlerTicketResearchEnrichment.

TDD-first: tests written before implementation.
Verifies context request sends correct ticket metadata and
context is written to .onex_state/knowledge-context/<ticket_id>.md.
Timeout >10s must not block research phase.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from omnimarket.nodes.node_ticket_research_enrichment_compute.handlers.handler_ticket_research_enrichment import (
    HandlerTicketResearchEnrichment,
)
from omnimarket.nodes.node_ticket_research_enrichment_compute.models.model_ticket_research_enrichment_request import (
    ModelTicketResearchEnrichmentRequest,
)
from omnimarket.nodes.node_ticket_research_enrichment_compute.models.model_ticket_research_enrichment_result import (
    EnumEnrichmentStatus,
)


@pytest.fixture
def tmp_state_dir(tmp_path: Path) -> Path:
    state_dir = tmp_path / ".onex_state"
    state_dir.mkdir()
    return state_dir


@pytest.fixture
def request_fixture() -> ModelTicketResearchEnrichmentRequest:
    return ModelTicketResearchEnrichmentRequest(
        ticket_id="OMN-11941",
        repo="omnimarket",
        description="Create a knowledge context enrichment node for ticket-work research phase.",
        linked_files=(
            "src/omnimarket/nodes/node_ticket_work/handlers/handler_ticket_work.py",
        ),
        context_timeout_s=10.0,
    )


class TestHandlerTicketResearchEnrichment:
    """Unit tests for the enrichment handler."""

    def test_enrichment_writes_context_to_state_dir(
        self, request_fixture: ModelTicketResearchEnrichmentRequest, tmp_state_dir: Path
    ) -> None:
        mock_assembler = MagicMock()
        mock_assembler.assemble_context.return_value = (
            "# Context\nRelevant patterns found."
        )

        handler = HandlerTicketResearchEnrichment(context_assembler=mock_assembler)
        result = handler.handle(request_fixture, onex_state_dir=tmp_state_dir)

        assert result.status == EnumEnrichmentStatus.OK
        context_file = tmp_state_dir / "knowledge-context" / "OMN-11941.md"
        assert context_file.exists()
        content = context_file.read_text()
        assert "# Context" in content

    def test_enrichment_request_passes_correct_ticket_metadata(
        self, request_fixture: ModelTicketResearchEnrichmentRequest, tmp_state_dir: Path
    ) -> None:
        mock_assembler = MagicMock()
        mock_assembler.assemble_context.return_value = "# Context\nSome context."

        handler = HandlerTicketResearchEnrichment(context_assembler=mock_assembler)
        handler.handle(request_fixture, onex_state_dir=tmp_state_dir)

        call_kwargs = mock_assembler.assemble_context.call_args
        assert call_kwargs is not None
        args, kwargs = call_kwargs
        # Verify ticket_id, repo, and description were passed
        call_args_str = str(args) + str(kwargs)
        assert "OMN-11941" in call_args_str
        assert "omnimarket" in call_args_str

    def test_timeout_does_not_block_research(
        self, request_fixture: ModelTicketResearchEnrichmentRequest, tmp_state_dir: Path
    ) -> None:
        import time

        def slow_assemble(*args: object, **kwargs: object) -> str:
            time.sleep(30)  # exceeds any reasonable timeout
            return "# Context"

        mock_assembler = MagicMock()
        mock_assembler.assemble_context.side_effect = slow_assemble

        slow_request = request_fixture.model_copy(update={"context_timeout_s": 0.05})
        handler = HandlerTicketResearchEnrichment(context_assembler=mock_assembler)

        start = time.monotonic()
        result = handler.handle(slow_request, onex_state_dir=tmp_state_dir)
        elapsed = time.monotonic() - start

        assert result.status == EnumEnrichmentStatus.TIMEOUT
        assert elapsed < 5.0  # must complete well within 5s despite slow assembler

    def test_assembler_error_returns_error_status(
        self, request_fixture: ModelTicketResearchEnrichmentRequest, tmp_state_dir: Path
    ) -> None:
        mock_assembler = MagicMock()
        mock_assembler.assemble_context.side_effect = RuntimeError("assembler down")

        handler = HandlerTicketResearchEnrichment(context_assembler=mock_assembler)
        result = handler.handle(request_fixture, onex_state_dir=tmp_state_dir)

        assert result.status == EnumEnrichmentStatus.ERROR
        assert result.error_message is not None
        assert "assembler down" in result.error_message

    def test_no_assembler_returns_skipped(
        self, request_fixture: ModelTicketResearchEnrichmentRequest, tmp_state_dir: Path
    ) -> None:
        handler = HandlerTicketResearchEnrichment(context_assembler=None)
        result = handler.handle(request_fixture, onex_state_dir=tmp_state_dir)
        assert result.status == EnumEnrichmentStatus.SKIPPED

    def test_context_file_path_contains_ticket_id(
        self, request_fixture: ModelTicketResearchEnrichmentRequest, tmp_state_dir: Path
    ) -> None:
        mock_assembler = MagicMock()
        mock_assembler.assemble_context.return_value = "# Context\nSome content."

        handler = HandlerTicketResearchEnrichment(context_assembler=mock_assembler)
        result = handler.handle(request_fixture, onex_state_dir=tmp_state_dir)

        assert result.status == EnumEnrichmentStatus.OK
        assert result.context_path is not None
        assert "OMN-11941" in str(result.context_path)

    def test_linked_files_included_in_assembler_call(
        self, request_fixture: ModelTicketResearchEnrichmentRequest, tmp_state_dir: Path
    ) -> None:
        mock_assembler = MagicMock()
        mock_assembler.assemble_context.return_value = "# Context\nPatterns."

        handler = HandlerTicketResearchEnrichment(context_assembler=mock_assembler)
        handler.handle(request_fixture, onex_state_dir=tmp_state_dir)

        call_kwargs = mock_assembler.assemble_context.call_args
        assert call_kwargs is not None
        args, kwargs = call_kwargs
        call_args_str = str(args) + str(kwargs)
        assert "handler_ticket_work.py" in call_args_str
