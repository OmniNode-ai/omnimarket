# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed errors of opening a bus (OMN-26027)."""

from __future__ import annotations

from omnimarket.models.bus_connect import ModelBusConnectAttempt


class LabRunBusError(RuntimeError):
    """The bus could not be selected or opened."""


class BusConnectError(LabRunBusError):
    """No declared address of the broker started; ``attempts`` names each one and why."""

    def __init__(self, attempts: tuple[ModelBusConnectAttempt, ...]) -> None:
        self.attempts = attempts
        detail = "; ".join(f"{a.address}: {a.reason}" for a in attempts)
        super().__init__(f"the bus did not start at any declared address ({detail})")


class BusConnectDeadlineError(BusConnectError):
    """At least one address did not start inside the connect deadline."""
