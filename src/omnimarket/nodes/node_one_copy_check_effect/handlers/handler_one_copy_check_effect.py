# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the files the overlay names under the registry root and run the pure one-copy check over them (OMN-19998)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from omnimarket.handlers.emission_sink import ProtocolEmissionSink
from omnimarket.nodes.node_one_copy_check_compute.handlers.handler_one_copy_check import (
    HandlerOneCopyCheck,
    load_overlay,
)
from omnimarket.nodes.node_one_copy_check_compute.models import (
    ModelOneCopyCheckOverlay,
    ModelOneCopyCheckRequest,
    ModelOneCopyCheckResult,
)
from omnimarket.nodes.node_one_copy_check_effect.protocols.protocol_file_referrer_reader import (
    FileReferrerReader,
)

NODE_NAME = "node_one_copy_check_effect"


class HandlerOneCopyCheckEffect:
    """``handle(request) -> result``: the real backend of the compute node's reader, so a violation fails the floor's chain."""

    def __init__(
        self,
        root: Path,
        overlay: ModelOneCopyCheckOverlay | None = None,
        clock: Callable[[], datetime] | None = None,
        sink: ProtocolEmissionSink | None = None,
    ) -> None:
        loaded = overlay if overlay is not None else load_overlay()
        self._check = HandlerOneCopyCheck(
            loaded,
            FileReferrerReader(root, loaded.referrer_sources),
            clock,
            sink,
        )

    def handle(self, request: ModelOneCopyCheckRequest) -> ModelOneCopyCheckResult:
        return self._check.handle(request)
