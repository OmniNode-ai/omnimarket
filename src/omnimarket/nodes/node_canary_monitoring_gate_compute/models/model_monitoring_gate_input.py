# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelMonitoringGateInput — request payload for the canary monitoring gate (OMN-14735, B10).

Bundles the observed signal readings with the threshold specs to evaluate them
against. Self-contained (no live-bus/AWS dependency) so the gate can be
exercised in-process/in-test ahead of any real canary run.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_canary_monitoring_gate_compute.models.model_signal_reading import (
    ModelSignalReading,
)
from omnimarket.nodes.node_canary_monitoring_gate_compute.models.model_threshold_spec import (
    ModelThresholdSpec,
)


class ModelMonitoringGateInput(BaseModel):
    """Request payload: observed readings plus the thresholds to gate them against.

    Attributes:
        correlation_id: Optional correlation id echoed back on the result.
        readings: Observed signal samples for this evaluation window.
        thresholds: Threshold specs (see
            :class:`~...model_threshold_spec.ModelThresholdSpec`) — may be
            unresolved (A6 pending).
        rds_single_az_maintenance_window: True when the canary's single-AZ
            RDS instance (verified 2026-07-17: no HA) is in a known AWS
            maintenance event. Per the managed-staging plan (A6/B10), an RDS
            threshold breach during a declared maintenance window must be
            distinguished from a genuine canary failure so a maintenance
            event does not trigger a false abort. This flag carries that
            distinction; it never suppresses a non-RDS signal.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID | None = Field(
        default=None, description="Echoed back on the result for tracing."
    )
    readings: tuple[ModelSignalReading, ...] = Field(
        default=(), description="Observed signal samples for this evaluation window."
    )
    thresholds: tuple[ModelThresholdSpec, ...] = Field(
        default=(), description="Threshold specs to gate the readings against."
    )
    rds_single_az_maintenance_window: bool = Field(
        default=False,
        description=(
            "True when the single-AZ RDS instance is in a declared AWS "
            "maintenance event; downgrades an RDS-signal ABORT to WARN so a "
            "maintenance event does not read as a canary failure."
        ),
    )


__all__ = ["ModelMonitoringGateInput"]
