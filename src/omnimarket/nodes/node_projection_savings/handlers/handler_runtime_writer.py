# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Runtime dispatch entrypoint for the savings projection."""

from omnimarket.handlers.handler_projection_runtime_writer import (
    RuntimeProjectionWriter,
)
from omnimarket.nodes.node_projection_savings.handlers.handler_savings import (
    SavingsProjectionRunner,
)


class SavingsProjectionWriter(RuntimeProjectionWriter):
    """Apply the existing writer once for each runtime delivery."""

    runner_type = SavingsProjectionRunner
