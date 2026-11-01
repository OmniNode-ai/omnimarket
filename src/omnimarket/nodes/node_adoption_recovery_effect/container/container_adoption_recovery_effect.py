# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one place that builds this node's handlers."""

from collections.abc import Callable
from pathlib import Path

from ..handler import HandlerAdoptionRecovery


def build_handler_adoption_recovery(
    start: Callable[[Path], str | None],
) -> HandlerAdoptionRecovery:
    """Build the recovery handler with its injected launcher."""
    return HandlerAdoptionRecovery(start)
