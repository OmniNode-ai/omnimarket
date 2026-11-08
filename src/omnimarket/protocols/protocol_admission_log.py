# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where admission keeps its observe log: one record per decision, whatever the decision."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from omnimarket.models.model_start_admission import ModelAdmissionLogEntry


@runtime_checkable
class ProtocolAdmissionLog(Protocol):
    """An append-only log of admission decisions."""

    def record(self, entry: ModelAdmissionLogEntry) -> None:
        """Keep one entry; a log that cannot keep it raises."""
        ...
