# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Projection and read outcome, including the groups actually attempted."""

from ._base import ModelFrozen
from .model_host_drift_fleet_view import ModelHostDriftFleetView


class ModelHostDriftFleetResult(ModelFrozen):
    view: ModelHostDriftFleetView
    consumer_group: str
    events_read: int
    read_ok: bool
    error: str
    groups_used: tuple[str, ...]
