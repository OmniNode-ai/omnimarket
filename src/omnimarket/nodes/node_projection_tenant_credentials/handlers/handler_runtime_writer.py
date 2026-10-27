# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Runtime dispatch entrypoint for the tenant credentials projection."""

from omnimarket.handlers.handler_projection_runtime_writer import (
    RuntimeProjectionWriter,
)
from omnimarket.nodes.node_projection_tenant_credentials.handlers.handler_tenant_credentials_projection import (
    HandlerTenantCredentialsProjectionRunner,
)


class TenantCredentialsProjectionWriter(RuntimeProjectionWriter):
    """Apply the existing writer once for each runtime delivery."""

    runner_type = HandlerTenantCredentialsProjectionRunner
