# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The probe port of node_work_ledger_no_file_readings_effect: one method per reading."""

from __future__ import annotations

from typing import Protocol

from omnimarket.nodes.node_work_ledger_no_file_readings_effect.models import (
    ModelAbsenceFacts,
    ModelCensusFacts,
    ModelFileProbeFacts,
    ModelPublishFacts,
    ModelRebuildFacts,
    ModelReceiptFacts,
    ModelRestoreFacts,
)


class ProtocolNoFileFacts(Protocol):
    """What each reading's probe saw. A probe that cannot run raises OSError or ValueError."""

    def file_probe(self) -> ModelFileProbeFacts: ...

    def census(self) -> ModelCensusFacts: ...

    def absence(self) -> ModelAbsenceFacts: ...

    def rebuild(self) -> ModelRebuildFacts: ...

    def restore(self) -> ModelRestoreFacts: ...

    def publish(self) -> ModelPublishFacts: ...

    def receipt(self) -> ModelReceiptFacts: ...


__all__ = ["ProtocolNoFileFacts"]
