# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The same per-host observation and installer boundary for local and remote drainers."""

from typing import Protocol

from omnimarket.models.silent_failure_alarm import (
    ModelDrainerObservation,
    ModelDrainerRepair,
)


class ProtocolDrainerWatch(Protocol):
    def observe(self) -> ModelDrainerObservation: ...

    def repair(self) -> ModelDrainerRepair: ...
