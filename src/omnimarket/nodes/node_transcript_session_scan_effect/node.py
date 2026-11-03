# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""NodeTranscriptSessionScanEffect - bound to its handler by contract (OMN-22259)."""

from __future__ import annotations

from collections.abc import Mapping

from omnibase_spi.protocols.storage import ProtocolTranscriptSource

from omnimarket.nodes.node_transcript_session_scan_effect.handlers.handler_transcript_session_scan import (
    HandlerTranscriptSessionScan,
)
from omnimarket.nodes.node_transcript_session_scan_effect.models import (
    ModelTranscriptScanOverlay,
)


class NodeTranscriptSessionScanEffect:
    """List transcript files through injected read-side stores under an overlay's roots."""

    def __init__(
        self,
        overlay: ModelTranscriptScanOverlay,
        sources: Mapping[str, ProtocolTranscriptSource],
    ) -> None:
        self.handler = HandlerTranscriptSessionScan(overlay=overlay, sources=sources)
