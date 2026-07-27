# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_staging_readiness_compute — typed staging-composition preflight (OMN-15253).

Pure COMPUTE over a caller-supplied snapshot. Live capture is slice 2's collect
EFFECT, which executes exactly the read-only probe list the composition
document's ``snapshot_sources`` declares.
"""

from omnimarket.nodes.node_staging_readiness_compute.handlers.handler_staging_readiness_compute import (
    HandlerStagingReadinessCompute,
)

__all__ = ["HandlerStagingReadinessCompute"]
