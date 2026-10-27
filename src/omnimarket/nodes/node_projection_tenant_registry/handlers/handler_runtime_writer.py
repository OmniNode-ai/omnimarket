# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Runtime dispatch entrypoint for the tenant registry projection."""

from omnimarket.handlers.handler_projection_runtime_writer import (
    RuntimeProjectionWriter,
)
from omnimarket.nodes.node_projection_tenant_registry.handlers.handler_tenant_registry_projection import (
    HandlerTenantRegistryProjectionRunner,
)


class TenantMirrorProjectionWriter(RuntimeProjectionWriter):
    """Apply the existing writer once for each runtime delivery."""

    runner_type = HandlerTenantRegistryProjectionRunner
