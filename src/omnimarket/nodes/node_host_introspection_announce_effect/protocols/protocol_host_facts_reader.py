# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one place the host introspection reads the machine (OMN-19998).

A reading that cannot be taken raises :class:`HostFactsUnreadableError` and yields no facts: the announcer publishes a
typed failure instead, so a reader downstream never takes a zero-filled host for a real one (the rule the capacity
reader holds for load, cores and memory).
"""

from __future__ import annotations

from typing import Protocol

from omnimarket.models.model_host_introspection_announcement import ModelHostFacts
from omnimarket.models.model_host_introspection_request import (
    ModelHostIntrospectionOverlay,
    ModelHostReadTargets,
)


class HostFactsUnreadableError(RuntimeError):
    """A fact the overlay asked for could not be read (a probe failed, timed out or answered nothing usable)."""


class ProtocolHostFactsReader(Protocol):
    def read(
        self, overlay: ModelHostIntrospectionOverlay, targets: ModelHostReadTargets
    ) -> ModelHostFacts: ...
