# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Runtime dispatch entrypoint for the registration projection."""

from omnimarket.handlers.handler_projection_runtime_writer import (
    RuntimeProjectionWriter,
)
from omnimarket.nodes.node_projection_registration.handlers.handler_registration import (
    RegistrationProjectionRunner,
)


class RegistrationProjectionWriter(RuntimeProjectionWriter):
    """Apply the existing writer once for each runtime delivery."""

    runner_type = RegistrationProjectionRunner
