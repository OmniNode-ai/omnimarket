# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A bounded read and fold request; replay discards the prior projection."""

from pydantic import AwareDatetime

from ._base import ModelFrozen
from .model_host_drift_fleet_overlay import ModelHostDriftFleetOverlay
from .model_host_drift_fleet_view import ModelHostDriftFleetView


class ModelHostDriftFleetRequest(ModelFrozen):
    host: str
    previous: ModelHostDriftFleetView | None
    now: AwareDatetime
    overlay: ModelHostDriftFleetOverlay | None = None
    replay: bool = False
