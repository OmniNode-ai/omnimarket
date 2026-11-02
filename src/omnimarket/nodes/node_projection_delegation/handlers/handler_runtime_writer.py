# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Runtime dispatch entrypoint for the delegation projection."""

from omnimarket.handlers.handler_projection_runtime_writer import (
    RuntimeProjectionWriter,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
    DelegationProjectionRunner,
)


class DelegationProjectionWriter(RuntimeProjectionWriter):
    """Apply the existing delegation writer once for each runtime delivery."""

    runner_type = DelegationProjectionRunner
