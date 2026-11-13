# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_dev_roll_failure_capture_effect (OMN-24013).

Consumes the dev roll manifest and incident the deploy agent publishes on the
dev bus lane, keeps the manifest and the failed container's log in an injected
artifact store, and publishes one typed roll terminal per incident. An
incident that cannot be read, has no issuer, or whose artifacts cannot be
written yields a terminal with outcome ``unknown`` and a typed reason: never a
success-like outcome.
"""

from omnimarket.nodes.node_dev_roll_failure_capture_effect.handlers import (
    HandlerDevRollIncidentCapture,
    HandlerDevRollManifestRecord,
)

__all__ = [
    "HandlerDevRollIncidentCapture",
    "HandlerDevRollManifestRecord",
]
