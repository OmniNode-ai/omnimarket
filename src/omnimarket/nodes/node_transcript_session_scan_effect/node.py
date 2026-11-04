# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""NodeTranscriptSessionScanEffect - bound to its handler by contract (OMN-22259)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from omnimarket.nodes.node_transcript_session_scan_effect.handlers.handler_transcript_session_scan import (
    HandlerTranscriptSessionScan,
)
from omnimarket.nodes.node_transcript_session_scan_effect.models import (
    ModelTranscriptScanOverlay,
)

# The injected store is structural; the port is needed only for annotations.
if TYPE_CHECKING:
    from omnibase_spi.protocols.storage import ProtocolTranscriptSource


class NodeTranscriptSessionScanEffect:
    """List transcript files through injected read-side stores under an overlay's roots."""

    def __init__(
        self,
        overlay: ModelTranscriptScanOverlay,
        sources: Mapping[str, ProtocolTranscriptSource],
    ) -> None:
        self.handler = HandlerTranscriptSessionScan(overlay=overlay, sources=sources)
