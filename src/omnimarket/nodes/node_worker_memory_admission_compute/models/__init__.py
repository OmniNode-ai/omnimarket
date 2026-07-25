# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed I/O models for node_worker_memory_admission_compute (OMN-14977)."""

from omnimarket.nodes.node_worker_memory_admission_compute.models.model_worker_memory_admission import (
    EnumMemoryAdmissionOutcome,
    EnumMemoryAdmissionRefusalReason,
    EnumMidRunCollapsePolicy,
    ModelHostMemoryAdvertisement,
    ModelMemoryAdmissionReceipt,
    ModelMemoryAdmissionRequest,
    should_collapse,
)

__all__ = [
    "EnumMemoryAdmissionOutcome",
    "EnumMemoryAdmissionRefusalReason",
    "EnumMidRunCollapsePolicy",
    "ModelHostMemoryAdvertisement",
    "ModelMemoryAdmissionReceipt",
    "ModelMemoryAdmissionRequest",
    "should_collapse",
]
