# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The per-merge running evidence behind the merged-and-running count, so the factory floor can forward it to the compute."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from omnimarket.models.model_merged_running_count import ModelMrcEvidenceRead


@runtime_checkable
class ProtocolMergedRunningEvidenceReader(Protocol):
    """Read the records that say a merge runs on dev (adoption rows, deploy receipts) as of ``now``.

    The answer carries the evidence and, when a source of it could not be read, the cause: a reader that read only part
    of its sources says so, and the caller then treats the evidence as unread rather than as complete.
    """

    def read_evidence(self, now: datetime) -> ModelMrcEvidenceRead:
        """Raise when the sources cannot be built at all."""
        ...


__all__: list[str] = ["ProtocolMergedRunningEvidenceReader"]
