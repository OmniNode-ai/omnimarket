# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerTicketResearchEnrichment — queries knowledge context assembler for research enrichment.

Dispatched during the RESEARCH phase of ticket-work to pre-populate a context
file at .onex_state/knowledge-context/<ticket_id>.md before proceeding to spec.
A timeout > context_timeout_s causes a non-blocking TIMEOUT result so research
can continue without context. The assembler call runs in a daemon thread so a
slow or hanging assembler never blocks the calling phase.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from queue import Empty, Queue
from typing import Protocol, runtime_checkable

from omnimarket.nodes.node_ticket_research_enrichment_compute.models.model_ticket_research_enrichment_request import (
    ModelTicketResearchEnrichmentRequest,
)
from omnimarket.nodes.node_ticket_research_enrichment_compute.models.model_ticket_research_enrichment_result import (
    EnumEnrichmentStatus,
    ModelTicketResearchEnrichmentResult,
)

_log = logging.getLogger(__name__)

_KNOWLEDGE_CONTEXT_SUBDIR = "knowledge-context"

_SENTINEL_TIMEOUT = object()
_SENTINEL_ERROR = object()


@runtime_checkable
class ProtocolContextAssembler(Protocol):
    """Minimal protocol for a knowledge context assembler."""

    def assemble_context(
        self,
        ticket_id: str,
        repo: str,
        description: str,
        linked_files: tuple[str, ...],
    ) -> str:
        """Return assembled context markdown string."""
        ...


class HandlerTicketResearchEnrichment:
    """Query a knowledge context assembler and write results for ticket research.

    When context_assembler is None, the handler skips enrichment (SKIPPED status).
    Timeout exceeding request.context_timeout_s yields TIMEOUT status without
    blocking the calling phase.
    """

    def __init__(
        self, context_assembler: ProtocolContextAssembler | None = None
    ) -> None:
        self._assembler = context_assembler

    def handle(
        self,
        request: ModelTicketResearchEnrichmentRequest,
        onex_state_dir: Path | None = None,
    ) -> ModelTicketResearchEnrichmentResult:
        """Run enrichment: query assembler, write context file, return result."""
        if self._assembler is None:
            _log.info(
                "[research-enrichment] no assembler configured — skipping for %s",
                request.ticket_id,
            )
            return ModelTicketResearchEnrichmentResult(
                status=EnumEnrichmentStatus.SKIPPED
            )

        state_dir = onex_state_dir or _resolve_onex_state_dir()
        context_dir = state_dir / _KNOWLEDGE_CONTEXT_SUBDIR
        context_dir.mkdir(parents=True, exist_ok=True)
        context_file = context_dir / f"{request.ticket_id}.md"

        result_queue: Queue[object] = Queue()
        assembler = self._assembler

        def _worker() -> None:
            try:
                ctx = assembler.assemble_context(
                    request.ticket_id,
                    request.repo,
                    request.description,
                    request.linked_files,
                )
                result_queue.put(ctx)
            except Exception as exc:
                result_queue.put((_SENTINEL_ERROR, exc))

        t = threading.Thread(target=_worker, daemon=True)
        t.start()

        try:
            item = result_queue.get(timeout=request.context_timeout_s)
        except Empty:
            _log.warning(
                "[research-enrichment] context assembler timed out after %.1fs for %s — proceeding without context",
                request.context_timeout_s,
                request.ticket_id,
            )
            return ModelTicketResearchEnrichmentResult(
                status=EnumEnrichmentStatus.TIMEOUT,
                error_message=f"Context assembler timed out after {request.context_timeout_s}s",
            )

        if isinstance(item, tuple) and len(item) == 2 and item[0] is _SENTINEL_ERROR:
            exc = item[1]
            _log.warning(
                "[research-enrichment] assembler error for %s: %s",
                request.ticket_id,
                exc,
            )
            return ModelTicketResearchEnrichmentResult(
                status=EnumEnrichmentStatus.ERROR,
                error_message=str(exc),
            )

        context_md = str(item)
        context_file.write_text(context_md, encoding="utf-8")
        _log.info("[research-enrichment] context written to %s", context_file)
        return ModelTicketResearchEnrichmentResult(
            status=EnumEnrichmentStatus.OK,
            context_path=context_file,
        )


def _resolve_onex_state_dir() -> Path:
    raw = os.environ.get("ONEX_STATE_DIR", "")
    if not raw:
        raise RuntimeError(
            "ONEX_STATE_DIR is not set; cannot determine knowledge-context output path"
        )
    return Path(raw)


__all__: list[str] = ["HandlerTicketResearchEnrichment", "ProtocolContextAssembler"]
