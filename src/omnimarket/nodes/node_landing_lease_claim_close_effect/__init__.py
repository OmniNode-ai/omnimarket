# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Close eligible released lease claims once, recording refused writes for retry."""

from pathlib import Path

from .handlers.handler_landing_lease_claim_close import HandlerLandingLeaseClaimClose


class NodeLandingLeaseClaimCloseEffect(HandlerLandingLeaseClaimClose):
    """ONEX entry point for the local handler."""

    contract_path = Path(__file__).with_name("contract.yaml")


__all__ = ["HandlerLandingLeaseClaimClose", "NodeLandingLeaseClaimCloseEffect"]
