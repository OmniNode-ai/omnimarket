# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Multi-parameter integration proof for node_ticket_research_enrichment_compute.

OMN-13679, WS-5. Variant A (COMPUTE, direct in-process handler call).
``HandlerTicketResearchEnrichment`` queries a knowledge-context assembler and
writes the assembled markdown to ``<onex_state>/knowledge-context/<ticket>.md``.
The assembler is a constructor-injected ``ProtocolContextAssembler`` collaborator
and ``onex_state_dir`` is passed to ``handle`` directly, so the test exercises
the real threading / timeout / file-write logic against a ``tmp_path`` with no
environment dependency and no monkeypatching.

Param axes: a healthy assembler (OK + file written), a multi-source request
(linked_files propagated to the assembler), no assembler (SKIPPED), a slow
assembler (TIMEOUT), and a NEGATIVE CONTROL raising assembler (ERROR + message).
"""

from __future__ import annotations

import time
from pathlib import Path

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


class _StaticAssembler:
    """Returns a fixed context body and records the args it was called with."""

    def __init__(self, body: str = "# Context\nsynthetic body") -> None:
        self._body = body
        self.calls: list[tuple[str, str, str, tuple[str, ...]]] = []

    def assemble_context(
        self,
        ticket_id: str,
        repo: str,
        description: str,
        linked_files: tuple[str, ...],
    ) -> str:
        self.calls.append((ticket_id, repo, description, linked_files))
        return self._body


class _RaisingAssembler:
    """Raises to exercise the ERROR path."""

    def assemble_context(
        self,
        ticket_id: str,
        repo: str,
        description: str,
        linked_files: tuple[str, ...],
    ) -> str:
        raise RuntimeError("assembler exploded")


class _SlowAssembler:
    """Sleeps past the request timeout to exercise the TIMEOUT path."""

    def assemble_context(
        self,
        ticket_id: str,
        repo: str,
        description: str,
        linked_files: tuple[str, ...],
    ) -> str:
        time.sleep(1.0)
        return "# Context\nnever returned in time"


@pytest.mark.integration
def test_enrichment_ok_writes_context_file(tmp_path: Path) -> None:
    assembler = _StaticAssembler(body="# OMN-13679\nresearch context")
    handler = HandlerTicketResearchEnrichment(context_assembler=assembler)
    request = ModelTicketResearchEnrichmentRequest(
        ticket_id="OMN-13679", repo="omnimarket", description="ws5 coverage"
    )

    result = handler.handle(request, onex_state_dir=tmp_path)

    assert result.status == EnumEnrichmentStatus.OK
    assert result.context_path is not None
    assert result.context_path == tmp_path / "knowledge-context" / "OMN-13679.md"
    assert (
        result.context_path.read_text(encoding="utf-8")
        == "# OMN-13679\nresearch context"
    )
    assert result.error_message is None


@pytest.mark.integration
def test_enrichment_propagates_linked_files_multisource(tmp_path: Path) -> None:
    assembler = _StaticAssembler()
    handler = HandlerTicketResearchEnrichment(context_assembler=assembler)
    linked = ("src/a.py", "src/b.py", "docs/c.md")
    request = ModelTicketResearchEnrichmentRequest(
        ticket_id="OMN-1",
        repo="omnimarket",
        description="multi-source",
        linked_files=linked,
    )

    result = handler.handle(request, onex_state_dir=tmp_path)

    assert result.status == EnumEnrichmentStatus.OK
    assert len(assembler.calls) == 1
    called_ticket, called_repo, _called_desc, called_linked = assembler.calls[0]
    assert called_ticket == "OMN-1"
    assert called_repo == "omnimarket"
    assert called_linked == linked


@pytest.mark.integration
def test_enrichment_no_assembler_is_skipped(tmp_path: Path) -> None:
    handler = HandlerTicketResearchEnrichment(context_assembler=None)
    request = ModelTicketResearchEnrichmentRequest(ticket_id="OMN-2", repo="omnimarket")

    result = handler.handle(request, onex_state_dir=tmp_path)

    assert result.status == EnumEnrichmentStatus.SKIPPED
    assert result.context_path is None


@pytest.mark.integration
def test_enrichment_timeout_does_not_block(tmp_path: Path) -> None:
    handler = HandlerTicketResearchEnrichment(context_assembler=_SlowAssembler())
    request = ModelTicketResearchEnrichmentRequest(
        ticket_id="OMN-3",
        repo="omnimarket",
        context_timeout_s=0.05,
    )

    result = handler.handle(request, onex_state_dir=tmp_path)

    assert result.status == EnumEnrichmentStatus.TIMEOUT
    assert result.context_path is None
    assert result.error_message is not None
    assert "timed out" in result.error_message


@pytest.mark.integration
def test_enrichment_assembler_error_is_a_finding(tmp_path: Path) -> None:
    # NEGATIVE CONTROL: a failing assembler must surface a typed ERROR result.
    handler = HandlerTicketResearchEnrichment(context_assembler=_RaisingAssembler())
    request = ModelTicketResearchEnrichmentRequest(ticket_id="OMN-4", repo="omnimarket")

    result = handler.handle(request, onex_state_dir=tmp_path)

    assert result.status == EnumEnrichmentStatus.ERROR
    assert result.context_path is None
    assert result.error_message is not None
    assert "assembler exploded" in result.error_message
