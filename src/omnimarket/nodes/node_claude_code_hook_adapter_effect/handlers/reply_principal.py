# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The requester's reply principal for per-principal reply topics (OMN-22103).

The compaction binding awaits topics templated on the principal the requester
publishes as. That principal is the SASL username of the dev lane's client
transport, the same identity the broker authorizes the requester's reads and
writes by, so a reply topic named for it is readable by that principal's
credential only. The resolver names no credential value and raises a plain
message when the lane has no username.
"""

from __future__ import annotations

from omnibase_infra.cli.delegate_lane import DelegateLaneSelectionError
from omnibase_infra.cli.delegate_lane_credentials import (
    DelegateLaneCredentialError,
    resolve_lane_binding,
)

REPLY_PRINCIPAL_LANE = "dev"


class ReplyPrincipalResolver:
    """resolve() -> the dev lane's broker username."""

    def resolve(self) -> str:
        try:
            _, transport = resolve_lane_binding(REPLY_PRINCIPAL_LANE)
        except (DelegateLaneSelectionError, DelegateLaneCredentialError) as exc:
            raise RuntimeError(
                f"no principal: the {REPLY_PRINCIPAL_LANE} lane did not resolve "
                f"({type(exc).__name__})"
            ) from None
        principal = getattr(transport, "sasl_username", None)
        if not isinstance(principal, str) or not principal:
            raise RuntimeError(
                f"no principal: the {REPLY_PRINCIPAL_LANE} lane resolves no broker username"
            )
        return principal


__all__ = ["REPLY_PRINCIPAL_LANE", "ReplyPrincipalResolver"]
