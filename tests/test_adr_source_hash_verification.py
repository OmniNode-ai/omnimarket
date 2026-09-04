# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Source-byte verification at the ingestion-to-segmentation trust boundary."""

from __future__ import annotations

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.adapters.adr.bus_protocol_adapters import HandlerBusAdrSegmentation
from omnimarket.models.adr import ModelAdrDocumentRef, ModelAdrIngestionResult


@pytest.mark.unit
async def test_changed_source_bytes_fail_before_segmentation_request(tmp_path) -> None:
    """A durable candidate hash can only originate from the verified source bytes."""
    source = tmp_path / "docs" / "plan.md"
    source.parent.mkdir()
    source.write_text("# Changed plan\n", encoding="utf-8", newline="")
    handler = HandlerBusAdrSegmentation({"event_bus": EventBusInmemory()})

    result = await handler.segment(
        ingestion=ModelAdrIngestionResult(
            root_paths=[str(tmp_path)],
            documents=[
                ModelAdrDocumentRef(
                    source_path="docs/plan.md",
                    source_content_sha256="0" * 64,
                )
            ],
        ),
        correlation_id="test-source-hash",
    )

    assert result.success is False
    assert result.error_code == "SOURCE_DOCUMENT_HASH_MISMATCH"
