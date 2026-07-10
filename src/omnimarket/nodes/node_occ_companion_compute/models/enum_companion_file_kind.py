# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""EnumCompanionFileKind — the kinds of net-new OCC companion files a plan emits.

Part of the RSD-1 COMPUTE seam (OMN-14285). Each file the deterministic plan
emits is one of these kinds; every one is net-new (an ``A`` in the OCC diff, or a
net-new supersession file), never a mutation of an existing merged receipt.
"""

from __future__ import annotations

from enum import StrEnum


class EnumCompanionFileKind(StrEnum):
    """Kind of a single companion file in a :class:`ModelOccCompanionPlan`."""

    CONTRACT = "contract"
    DOWNSTREAM_RECEIPT = "downstream_receipt"
    SELF_BIND_RECEIPT = "self_bind_receipt"
    SUPERSEDE_RECEIPT = "supersede_receipt"


__all__ = ["EnumCompanionFileKind"]
