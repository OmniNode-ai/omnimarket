# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of the lab proof judge node: the substitute-receipt store."""

from __future__ import annotations

from typing import Protocol

from omnimarket.nodes.node_lab_proof_judge_compute.models.model_substitute_receipt import (
    ModelSubstituteReceipt,
)


class ProtocolReceiptStore(Protocol):
    """Read and write substitute receipts for the lab proof judge."""

    def read(self, repo: str, node: str) -> tuple[ModelSubstituteReceipt, ...]:
        """Return the receipts stored for ``repo`` and ``node``."""
        ...

    def read_for_head(self, repo: str, head: str) -> tuple[ModelSubstituteReceipt, ...]:
        """Return every receipt of ``repo`` at exactly ``head``."""
        ...

    def write(self, receipt: ModelSubstituteReceipt) -> str:
        """Persist ``receipt`` and return its storage path."""
        ...
