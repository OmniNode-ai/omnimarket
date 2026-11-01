# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one place that builds node_cause_claim_ownership_effect handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from omnimarket.nodes.node_cause_claim_ownership_effect.handler import (
        HandlerCauseClaimOwnership,
    )
    from omnimarket.nodes.node_exception_queue_effect.handler import (
        HandlerExceptionEmit,
    )


def build_handler_cause_claim_ownership() -> HandlerCauseClaimOwnership:
    """Build the ownership handler on demand."""
    from omnimarket.nodes.node_cause_claim_ownership_effect.handler import (
        HandlerCauseClaimOwnership,
    )

    return HandlerCauseClaimOwnership()


def build_handler_exception_emit() -> HandlerExceptionEmit:
    """Build the exception emitter on demand."""
    from omnimarket.nodes.node_exception_queue_effect.handler import (
        HandlerExceptionEmit,
    )

    return HandlerExceptionEmit()
