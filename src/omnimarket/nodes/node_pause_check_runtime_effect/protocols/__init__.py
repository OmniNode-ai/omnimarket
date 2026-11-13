# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The injected dependencies of the pause check findings runtime."""

from __future__ import annotations

from typing import Protocol

from omnimarket.nodes.node_pause_check_findings_compute.models import (
    ModelPauseCheckEvent,
)


class ProtocolPauseCheckEventSink(Protocol):
    def emit(self, event: ModelPauseCheckEvent) -> None: ...


__all__ = ["ProtocolPauseCheckEventSink"]
