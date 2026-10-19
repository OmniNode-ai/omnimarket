# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Requests and readings of the host thermal observe node."""

from .model_host_thermal_observe_request import ModelHostThermalObserveRequest
from .model_host_thermal_reading import ModelHostGpuReading, ModelHostThermalReading

__all__ = [
    "ModelHostGpuReading",
    "ModelHostThermalObserveRequest",
    "ModelHostThermalReading",
]
