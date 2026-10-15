# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab work unit models as the shared lab-work caller surface (OMN-17427).

Callers outside ``node_lab_work_unit_effect`` (the morning orchestrator's lab
work phases) type their requests, receipts and capacity reads from here, the
same seam ``omnimarket.lab_work`` already uses, instead of reaching into the
effect node's models package.
"""

from omnimarket.nodes.node_lab_work_unit_effect.models import (
    MAX_TAIL_CHARS,
    MAX_TIMEOUT_SECONDS,
    EnumLabWorkUnitStatus,
    ModelHostCapacityAdvertisement,
    ModelHostCapacityProbeRequest,
    ModelLabWorkUnitReceipt,
    ModelLabWorkUnitRequest,
    WorkKind,
)

__all__ = [
    "MAX_TAIL_CHARS",
    "MAX_TIMEOUT_SECONDS",
    "EnumLabWorkUnitStatus",
    "ModelHostCapacityAdvertisement",
    "ModelHostCapacityProbeRequest",
    "ModelLabWorkUnitReceipt",
    "ModelLabWorkUnitRequest",
    "WorkKind",
]
