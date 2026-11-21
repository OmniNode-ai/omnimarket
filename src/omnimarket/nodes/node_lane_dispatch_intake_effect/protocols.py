# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of the lane dispatcher: the brief builder, the lane placer and the ledger row writer."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from omnimarket.nodes.node_lane_dispatch_intake_effect.models.model_lane_dispatch_intake import (
    ModelPlacementOutcome,
    ModelPlacementRequest,
)


class ProtocolBriefBuilder(Protocol):
    def build(self, request: ModelPlacementRequest) -> Path:
        """Write and check the lane's brief; return its path. Raises ``BriefError`` when no brief passes the check."""
        ...


class ProtocolLanePlacer(Protocol):
    def place(
        self, request: ModelPlacementRequest, brief: Path, key: str, parent: str
    ) -> ModelPlacementOutcome:
        """Place one lane detached and return how it ended; the call returns when the placement is decided."""
        ...


class ProtocolRowWriter(Protocol):
    def append(self, row: str) -> None:
        """Append one ledger row; raises ``RowWriteError`` when the ledger refuses it."""
        ...


class BriefError(RuntimeError):
    """The brief could not be built or did not pass the lane-brief check."""


class RowWriteError(RuntimeError):
    """The ledger refused the row."""
