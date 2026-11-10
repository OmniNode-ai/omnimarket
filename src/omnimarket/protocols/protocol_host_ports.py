# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Injected boundaries for measuring a host and acting on it."""

from typing import Protocol

from omnimarket.models.model_host_profile import (
    ModelGapAction,
    ModelHostMeasurement,
    ModelHostProfile,
)


class ProtocolHostMeasurement(Protocol):
    """Reads what the host named by the profile carries; read-only."""

    def measure(self, profile: ModelHostProfile) -> ModelHostMeasurement: ...


class ProtocolHostActions(Protocol):
    """Carries out one planned action on the host named by the profile."""

    def apply(self, profile: ModelHostProfile, action: ModelGapAction) -> None: ...
