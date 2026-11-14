# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_lab_fill_launcher_effect: the morning-workflows tick's judge of one lab-fill fire (OMN-24109).

Reads the fire's files, judges them with the pure compute node
``node_lab_fill_fire_outcome_compute``, delivers the alert it names, publishes
the validated result and prints one JSON line with the verdict.
"""

from omnimarket.nodes.node_lab_fill_launcher_effect.handlers.handler_lab_fill_launcher import (
    HandlerLabFillLauncher,
)

__all__ = ["HandlerLabFillLauncher"]
