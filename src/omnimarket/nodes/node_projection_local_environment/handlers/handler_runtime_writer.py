# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Runtime dispatch entrypoint for the local environment projection."""

from omnimarket.handlers.handler_projection_runtime_writer import (
    RuntimeProjectionWriter,
)
from omnimarket.nodes.node_projection_local_environment.handlers.handler_local_environment_writer import (
    HandlerLocalEnvironmentProjectionRunner,
)


class LocalEnvironmentProjectionWriter(RuntimeProjectionWriter):
    """Apply the existing writer once for each runtime delivery."""

    runner_type = HandlerLocalEnvironmentProjectionRunner


__all__ = ["LocalEnvironmentProjectionWriter"]
