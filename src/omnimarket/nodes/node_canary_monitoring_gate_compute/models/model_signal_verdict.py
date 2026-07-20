# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelSignalVerdict — per-signal classification from the monitoring gate (OMN-14735, B10)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_canary_monitoring_gate_compute.models.model_signal_reading import (
    SignalName,
)

# UNRESOLVED means the reading has no fully-resolved threshold spec (A6 has
# not supplied both warn/abort numbers, or no spec was supplied at all) — it
# is a distinct, honest state, never conflated with PASS.
VerdictStatus = Literal["PASS", "WARN", "ABORT", "UNRESOLVED"]


class ModelSignalVerdict(BaseModel):
    """The classification of one observed reading against its threshold spec.

    Attributes:
        signal_name: Which monitoring signal domain this verdict covers.
        status: The classification outcome.
        value: The observed value that was classified.
        threshold_source: Provenance of the threshold used (or the
            unresolved sentinel — see
            :data:`~...model_threshold_spec.UNRESOLVED_SOURCE`).
        reason: Human-readable explanation for the status.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    signal_name: SignalName = Field(
        ..., description="Which monitoring signal domain this verdict covers."
    )
    status: VerdictStatus = Field(..., description="Classification outcome.")
    value: float = Field(..., description="The observed value that was classified.")
    threshold_source: str = Field(
        ..., description="Provenance of the threshold used, or the unresolved sentinel."
    )
    reason: str = Field(
        default="", description="Human-readable explanation for the status."
    )


__all__ = ["ModelSignalVerdict", "VerdictStatus"]
