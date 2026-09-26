# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the reconciler would do about a derived-versus-current state difference.

Related:
    - OMN-16729: Epic — board-truth mechanization
    - OMN-16731: node_board_truth_compute
    - OMN-16733: the live writer that consumes these actions (unarmed)
"""

from __future__ import annotations

from enum import StrEnum


class EnumBoardReconcileAction(StrEnum):
    """Reconciliation verdict for one ticket.

    NO_CHANGE: the derivation agrees with the board. This is what makes the
        reconciler idempotent — a second run over an unchanged board is all
        NO_CHANGE.
    FLIP: the derivation disagrees with the board and the disagreement is
        actionable. A FLIP row always carries citable evidence; a flip with no
        entailing fact is a bug, not a flip.
    DISCREPANCY: the derivation disagrees but must not be acted on — ambiguous
        facts, or a completion signal whose edge belongs to OMN-16106. Reported,
        never written.
    """

    NO_CHANGE = "no_change"
    FLIP = "flip"
    DISCREPANCY = "discrepancy"


__all__ = [
    "EnumBoardReconcileAction",
]
