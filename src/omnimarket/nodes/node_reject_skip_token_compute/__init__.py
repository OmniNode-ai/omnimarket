# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reject-skip-token compute: operating rule 10 as a pure scan of text.

``HandlerRejectSkipToken.handle(ModelSkipTokenScanRequest) -> ModelSkipTokenScanResult``
(definition-B). It refuses exactly what the vendored ``reject-deploy-gate-skip-token.sh`` refuses.
"""

from omnimarket.nodes.node_reject_skip_token_compute.handlers.handler_reject_skip_token import (
    HandlerRejectSkipToken,
)


class NodeRejectSkipTokenCompute(HandlerRejectSkipToken):
    """ONEX entry-point wrapper for HandlerRejectSkipToken."""


__all__ = ["HandlerRejectSkipToken", "NodeRejectSkipTokenCompute"]
