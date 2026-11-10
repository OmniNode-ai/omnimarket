# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The start-admission effect's log port: one row per decision, including what it would refuse."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from omnimarket.models.model_start_admission import ModelStartAdmissionLogRow


@runtime_checkable
class ProtocolStartAdmissionLog(Protocol):
    """Where the effect writes what it decided."""

    def append(self, row: ModelStartAdmissionLogRow) -> None:
        """Record one decision."""
        ...
