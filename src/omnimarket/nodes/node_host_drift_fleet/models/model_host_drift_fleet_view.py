# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Immutable fleet projection and its reader identity."""

from pydantic import AwareDatetime

from ._base import ModelFrozen
from .model_host_drift_fleet_host import ModelHostDriftFleetHost


class ModelHostDriftFleetView(ModelFrozen):
    hosts: tuple[ModelHostDriftFleetHost, ...]
    alerts: tuple[str, ...]
    status_line: str
    folded_at: AwareDatetime
    consumer_group: str
    malformed: int = 0
